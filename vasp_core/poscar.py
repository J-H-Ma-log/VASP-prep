#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""poscar.py —— POSCAR 的"无损"读写与结构操作（纯标准库）。

为什么单独写一个 POSCAR 解析器，而不用 ASE/pymatgen：
  【做】只用 Python 标准库实现 【因】目标机器是 RHEL6.5 + glibc2.12 + 无外网，
        装 pymatgen/ASE 需要几十个包、很多轮子还要求 glibc≥2.17
  【坑】自己写要处理 VASP4/VASP5、Direct/Cartesian、负 scale、Selective dynamics、
        velocities、CRLF/BOM，少处理一样就会"悄悄改错结构"
  【替】`--backend pymatgen` 时改用 pymatgen（见 pmg_backend.py），用于切表面等复杂操作

坐标约定（全文件统一，务必记住）：
  lattice 是 3 行（a、b、c 三个晶格矢量），每行一个向量；
  分数坐标 f、笛卡尔坐标 r 的关系是  r = f @ lattice（行向量右乘）。
  反过来 f = r @ inv(lattice)。

为什么强调这个：VASP 内部用分数坐标；扩胞、加真空在分数坐标下做最稳；
  【坑】如果矩阵乘法方向写反（lattice @ f），看起来也能跑，但结构会镜像/错位
  【替】用 numpy 更直观，但集群上不一定有 numpy（标准库没有矩阵乘）
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import StructureError

# 元素符号的粗校验：首字母大写 + 可选小写字母，例如 Cu、Mo、S、H
# 【做】只做格式校验 【因】真正的元素表在 POTCAR 里，这里不重复维护
# 【坑】写成 "CO"（钴 vs 一氧化碳）会被当成合法符号，所以要靠 POTCAR 的顺序校验兜底
_ELEMENT_RE = re.compile(r"^[A-Z][a-z]?$")


# ---------------------------------------------------------------------------
# 小工具：3x3 矩阵与向量（行向量约定）
# ---------------------------------------------------------------------------

def det3(m: list[list[float]]) -> float:
    """3x3 行列式。用于负 scale（按体积定义晶格）与体积检查。"""
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def inv3(m: list[list[float]]) -> list[list[float]]:
    """3x3 求逆（伴随矩阵法）。体积为 0 说明三个矢量共面，POSCAR 本身就坏了。"""
    d = det3(m)
    if abs(d) < 1e-12:
        raise StructureError("晶格矩阵行列式为 0（三个晶格矢量共面），无法求逆")
    return [
        [
            (m[1][1] * m[2][2] - m[1][2] * m[2][1]) / d,
            (m[0][2] * m[2][1] - m[0][1] * m[2][2]) / d,
            (m[0][1] * m[1][2] - m[0][2] * m[1][1]) / d,
        ],
        [
            (m[1][2] * m[2][0] - m[1][0] * m[2][2]) / d,
            (m[0][0] * m[2][2] - m[0][2] * m[2][0]) / d,
            (m[0][2] * m[1][0] - m[0][0] * m[1][2]) / d,
        ],
        [
            (m[1][0] * m[2][1] - m[1][1] * m[2][0]) / d,
            (m[0][1] * m[2][0] - m[0][0] * m[2][1]) / d,
            (m[0][0] * m[1][1] - m[0][1] * m[1][0]) / d,
        ],
    ]


def mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """3x3 乘 3x3。行向量约定下，c[i][j] = sum_k a[i][k] * b[k][j]。"""
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_T(a: list[list[float]]) -> list[list[float]]:
    return [[a[j][i] for j in range(3)] for i in range(3)]


def vec_mat(v: list[float], m: list[list[float]]) -> list[float]:
    """行向量 v 右乘矩阵 m：结果[j] = sum_i v[i] * m[i][j]。"""
    return [sum(v[i] * m[i][j] for i in range(3)) for j in range(3)]


def vec_add(a: list[float], b: list[float]) -> list[float]:
    return [a[i] + b[i] for i in range(3)]


def vec_scale(a: list[float], s: float) -> list[float]:
    return [x * s for x in a]


def vec_len(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _fmt(x: float) -> str:
    """统一数字格式：10 位小数，去掉 -0.0000000000。

    【因】diff 要稳定，否则同一个结构每次写出来小数位不同，人看不出改了啥
    【坑】用 str(float) 会写 0.30000000000000004 这种丑数
    """
    if abs(x) < 5e-11:
        x = 0.0
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _is_int_token(tok: str) -> bool:
    try:
        int(tok)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 结构对象
# ---------------------------------------------------------------------------

@dataclass
class Structure:
    """一份 POSCAR 的内存表示。

    字段刻意贴近文件格式，而不是"归一化后的物理量"，这样写回时可以保持原样：
      scale_raw / lattice_raw 保留原文件第二、三~五行；
      coords 保留原坐标模式（Direct 或 Cartesian）下的数值；
      flags/velocities 原样保留，避免"读进来再写出去，选择性弛豫没了"。
    """

    comment: str = ""
    scale_raw: float = 1.0                       # 第二行：>0 是长度因子，<0 是目标体积
    lattice_raw: list[list[float]] = field(default_factory=list)   # 3x3，未乘 scale
    species: list[str] | None = None              # VASP5 才有；VASP4 为 None
    counts: list[int] = field(default_factory=list)
    coords: list[list[float]] = field(default_factory=list)        # 原模式下的坐标
    coord_mode: str = "direct"                   # "direct" / "cartesian"
    selective: bool = False
    flags: list[list[bool]] | None = None        # 每原子 3 个 T/F（仅 selective 时）
    velocities: list[list[float]] | None = None  # 速度块（Å/fs，原样保留）
    tail: list[str] = field(default_factory=list)  # 速度块之后不认识的尾部，原样保留

    # ---- 基本属性 ----

    @property
    def natoms(self) -> int:
        return sum(self.counts)

    @property
    def scale_factor(self) -> float:
        """把 lattice_raw 变成 Å 的因子。

        VASP 规则：scale > 0 时晶格矢量乘 scale；scale < 0 时 |scale| 是目标体积，
        因子 = (目标体积 / 当前体积)^(1/3)。
        【坑】直接拿 scale 乘晶格，遇到负 scale 会得到负晶格、体积为负
        """
        if self.scale_raw > 0:
            return self.scale_raw
        if self.scale_raw < 0:
            v0 = abs(det3(self.lattice_raw))
            if v0 <= 0:
                raise StructureError("负 scale（按体积定义）但原始晶格体积为 0")
            return (abs(self.scale_raw) / v0) ** (1.0 / 3.0)
        raise StructureError("POSCAR 第二行的 scale 不能为 0")

    @property
    def lattice(self) -> list[list[float]]:
        """物理晶格（Å），行向量。"""
        s = self.scale_factor
        return [[s * x for x in row] for row in self.lattice_raw]

    def volume(self) -> float:
        return abs(det3(self.lattice))

    # ---- 坐标换算 ----

    def frac(self) -> list[list[float]]:
        """所有原子的分数坐标。

        Direct 模式：原值就是分数坐标。
        Cartesian 模式：r = scale * coord（Å），f = r @ inv(lattice)。
          注意 inv(scale*raw) = inv(raw)/scale，scale 会约掉，
          所以这里直接用 coord @ inv(raw) —— 这样 scale 取任何值都对。
        【坑】v1 的写法是 coord @ inv(scale*raw)，scale≠1 时坐标会整体缩小 scale 倍
        """
        if self.coord_mode == "direct":
            return [list(c) for c in self.coords]
        invr = inv3(self.lattice_raw)
        return [vec_mat(c, invr) for c in self.coords]

    def cart(self) -> list[list[float]]:
        """所有原子的笛卡尔坐标（Å）。"""
        L = self.lattice
        return [vec_mat(f, L) for f in self.frac()]

    def set_frac(self, fracs: list[list[float]]) -> None:
        """用分数坐标替换全部原子位置；按当前 coord_mode 转成文件里的数值。

        【坑】这里必须校验长度：扩胞/替换这类操作一旦把 counts 和坐标数量写岔，
              VASP 读到的原子数就和元素行对不上，但它**不会报错**，只会算错体系。
        """
        if len(fracs) != sum(self.counts):
            raise StructureError(
                f"坐标数量 {len(fracs)} 与元素计数 {self.counts}（共 {sum(self.counts)} 个原子）不一致"
            )
        if self.coord_mode == "direct":
            self.coords = [list(f) for f in fracs]
        else:
            L = self.lattice
            self.coords = [vec_mat(f, L) for f in fracs]

    # ---- 元素 ----

    def atom_species(self) -> list[str]:
        """展开成逐原子元素列表；VASP4（无元素行）时抛错，让调用方给 --elements。"""
        if self.species is None:
            raise StructureError(
                "这是 VASP4 格式的 POSCAR（没有元素行），无法知道每个原子是什么元素。\n"
                "请先用 VASP5 格式重写，或在配方里用 elements 指定元素顺序。"
            )
        out: list[str] = []
        for sym, n in zip(self.species, self.counts):
            out.extend([sym] * n)
        return out

    def has_species(self) -> bool:
        return self.species is not None

    def summary(self) -> dict:
        return {
            "natoms": self.natoms,
            "species": self.species,
            "counts": self.counts,
            "volume": round(self.volume(), 4),
            "coord_mode": self.coord_mode,
            "selective": self.selective,
            "has_velocities": self.velocities is not None,
            "scale_raw": self.scale_raw,
        }


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def read_poscar_text(text: str, *, source: str = "<text>") -> Structure:
    """把 POSCAR 文本解析成 Structure。逐行说明见文件末尾的"解析流程"。"""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 7:
        raise StructureError(f"{source}: 只有 {len(lines)} 行，不像 POSCAR")

    comment = lines[0].strip()

    # 第二行：scale。VASP 只读第一个数（后面可以跟注释）
    try:
        scale_raw = float(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise StructureError(f"{source}: 第二行不是数字：{lines[1]!r}") from exc

    # 第三~五行：三个晶格矢量；每行只取前 3 个数
    try:
        lattice_raw = [[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)]
    except (IndexError, ValueError) as exc:
        raise StructureError(f"{source}: 第 3~5 行不是 3x3 晶格矩阵") from exc
    if any(len(row) != 3 for row in lattice_raw):
        raise StructureError(f"{source}: 晶格矩阵某一行不足 3 个数")

    # 第六行：VASP5 是元素符号，VASP4 是原子数。
    # 判断依据：所有 token 都能转成整数 → 原子数行（VASP4）
    idx = 5
    toks = lines[idx].split()
    if not toks:
        raise StructureError(f"{source}: 第 6 行是空的")
    if all(_is_int_token(t) for t in toks):
        species = None
        counts = [int(t) for t in toks]
        idx = 6
    else:
        species = toks
        for sym in species:
            if not _ELEMENT_RE.match(sym):
                raise StructureError(f"{source}: 元素符号不合法：{sym!r}")
        try:
            counts = [int(x) for x in lines[idx + 1].split()]
        except (IndexError, ValueError) as exc:
            raise StructureError(f"{source}: 第 7 行不是原子数列表") from exc
        idx = 7

    if species is not None and len(counts) != len(species):
        raise StructureError(
            f"{source}: 元素 {len(species)} 个，原子数 {len(counts)} 个，数量对不上"
        )
    if any(n < 0 for n in counts):
        raise StructureError(f"{source}: 原子数不能为负：{counts}")
    natoms = sum(counts)
    if natoms == 0:
        raise StructureError(f"{source}: 一个原子都没有")

    # Selective dynamics 是可选的，必须在坐标模式行之前
    selective = False
    if idx < len(lines) and lines[idx].strip().lower().startswith("s"):
        selective = True
        idx += 1

    # 坐标模式行：Direct 或 Cartesian
    if idx >= len(lines):
        raise StructureError(f"{source}: 缺少坐标模式行（Direct/Cartesian）")
    mode_line = lines[idx].strip()
    first = mode_line[:1].lower()
    if first == "d":
        coord_mode = "direct"
    elif first in ("c", "k"):          # Cartesian / Kartesian，VASP 两种拼写都认
        coord_mode = "cartesian"
    else:
        raise StructureError(f"{source}: 不认识坐标模式 {mode_line!r}（应为 Direct/Cartesian）")
    idx += 1

    # 读 natoms 行坐标；selective 时每行后面还有 3 个 T/F
    coords: list[list[float]] = []
    flags: list[list[bool]] | None = [] if selective else None
    for k in range(natoms):
        if idx + k >= len(lines):
            raise StructureError(
                f"{source}: 声明 {natoms} 个原子，坐标只找到 {k} 个"
            )
        parts = lines[idx + k].split()
        if len(parts) < 3:
            raise StructureError(f"{source}: 第 {idx + k + 1} 行坐标不足 3 个数")
        try:
            coords.append([float(parts[0]), float(parts[1]), float(parts[2])])
        except ValueError as exc:
            raise StructureError(f"{source}: 第 {idx + k + 1} 行坐标不是数字") from exc
        if selective:
            toks_f = parts[3:6]
            if len(toks_f) < 3:
                # 某些工具只写一个 T/F 表示三个方向相同，这里兼容
                if len(toks_f) == 1:
                    toks_f = toks_f * 3
                else:
                    raise StructureError(
                        f"{source}: Selective dynamics 行的约束不足 3 个：{lines[idx + k]!r}"
                    )
            flags.append([t[:1].upper() == "T" for t in toks_f])
    idx += natoms

    # 可选的速度块：坐标之后、每原子 3 个数的块，行数 >= natoms
    # 【坑】真实文件经常在坐标和速度块之间夹一个空行，有的还带 "Velocities" 标题行；
    #      只看 lines[idx] 会以为"后面是空的"，于是速度块被当成 tail 原样写回去——
    #      ASE 读这种文件会按"速度块原子数不对"直接断言失败，VASP 也可能读错。
    velocities: list[list[float]] | None = None
    rest_all = lines[idx:]
    k0 = 0
    while k0 < len(rest_all) and not rest_all[k0].strip():
        k0 += 1
    if k0 < len(rest_all) and rest_all[k0].strip().lower() in ("velocities", "velocity"):
        k0 += 1
        while k0 < len(rest_all) and not rest_all[k0].strip():
            k0 += 1
    rest = rest_all[k0:]
    if rest:
        probe = rest[0].split()
        if len(probe) >= 3 and all(_is_float_token(t) for t in probe[:3]) and len(rest) >= natoms:
            vel = []
            ok = True
            for k in range(natoms):
                parts = rest[k].split()
                if len(parts) < 3 or not all(_is_float_token(t) for t in parts[:3]):
                    ok = False
                    break
                vel.append([float(parts[0]), float(parts[1]), float(parts[2])])
            if ok:
                velocities = vel
                idx += k0 + natoms

    # 尾部只保留"不像数字"的行（注释/标记）；数字行是没识别出来的速度块，不能原样带走
    tail = []
    for ln in lines[idx:]:
        if not ln.strip():
            continue
        first = ln.split()[0]
        if _is_float_token(first):
            continue
        tail.append(ln)

    st = Structure(
        comment=comment,
        scale_raw=scale_raw,
        lattice_raw=lattice_raw,
        species=species,
        counts=counts,
        coords=coords,
        coord_mode=coord_mode,
        selective=selective,
        flags=flags,
        velocities=velocities,
        tail=tail,
    )
    # 解析时就做一次自检：体积必须为正，否则后面所有操作都没有物理意义
    if st.volume() <= 0:
        raise StructureError(f"{source}: 晶格体积不为正，POSCAR 可能已损坏")

    # 同元素在元素行里重复（如 Mo S Mo S）时，合并成"每个元素只出现一次"。
    # 【因】VASP 的 POTCAR 是按元素行逐段读的：元素行有 4 段就必须拼 4 段 POTCAR。
    #      不合并的话，我们生成的 POTCAR.spec（2 段）和 POSCAR（4 段）对不上，
    #      VASP 会报 "number of POTCAR files does not match"；而合并后化学上完全等价。
    # 【坑】合并必须同时重排原子并带动 Selective/速度，否则约束会错位到别的原子上。
    if species is not None and len(set(species)) != len(species):
        st = _regroup(st, st.atom_species(), st.frac(), st.flags, st.velocities)
    return st


def _is_float_token(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def read_poscar(path: str | Path) -> Structure:
    """从文件读 POSCAR；兼容 UTF-8 BOM、CRLF、GBK。"""
    p = Path(path)
    if not p.exists():
        raise StructureError(f"找不到 POSCAR：{p}")
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 永远能解
        raise StructureError(f"{p}: 无法解码文本")
    return read_poscar_text(text, source=str(p))


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def write_poscar_text(st: Structure, comment: str | None = None) -> str:
    """把 Structure 写成 POSCAR 文本。

    写回策略（为什么这样选）：
      - 统一 scale = 1.0，晶格直接用 Å：避免浮点 scale 带来的二次误差；
      - 保留原坐标模式（Direct 还是 Cartesian）：减少 diff 噪音；
      - 保留 Selective dynamics 与 velocities：这是 v1 最大的数据丢失点；
      - 元素行、原子数行总是重新生成：这是唯一能保证"结构变了、头也跟着变"的方式。
    """
    lines: list[str] = []
    lines.append((comment if comment is not None else st.comment) or "generated by vasp-prep")
    lines.append("1.0")
    L = st.lattice
    for row in L:
        lines.append("  " + "  ".join(_fmt(x) for x in row))

    if st.species is not None:
        lines.append("  " + "  ".join(st.species))
    lines.append("  " + "  ".join(str(n) for n in st.counts))

    if st.selective:
        lines.append("Selective dynamics")
    lines.append("Direct" if st.coord_mode == "direct" else "Cartesian")

    fracs = st.frac()
    for i, f in enumerate(fracs):
        if st.coord_mode == "direct":
            xyz = f
        else:
            xyz = vec_mat(f, L)
        line = "  " + "  ".join(_fmt(x) for x in xyz)
        if st.selective:
            flags = st.flags[i] if st.flags else [False, False, False]
            line += "   " + "   ".join("T" if b else "F" for b in flags)
        lines.append(line)

    # 速度块：坐标后先留一个空行，再写每原子 3 个数。
    # 【因】ASE 的 read_vasp 会先读一行判断是不是 "Lattice velocities"：
    #      没有空行时，它把第 1 个原子的速度当判断行吞掉，接着再读 natoms 行就 EOF → assert 失败；
    #      有空行时，判断行是空行，随后正好读满 natoms 行。VASP 本身也接受空行。
    if st.velocities is not None and len(st.velocities) == st.natoms:
        lines.append("")
        for v in st.velocities:
            lines.append("  " + "  ".join(_fmt(x) for x in v))

    if st.tail:
        lines.extend(st.tail)

    return "\n".join(lines) + "\n"


def write_poscar(st: Structure, path: str | Path, comment: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(write_poscar_text(st, comment), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 结构操作（全部返回新对象，不改原对象 —— 和 MatSimPy 的不可变约定一致）
# ---------------------------------------------------------------------------

def _check_indices(indices: int | list[int], natoms: int, what: str = "索引") -> list[int]:
    """把 1 基索引规范成 0 基列表，并检查范围。

    【因】用户和模型都习惯"第几个原子"从 1 数；内部用 0 基
    【坑】越界不报错、直接取 list[i] 会 IndexError 或（更糟）悄悄取错原子
    """
    if isinstance(indices, int):
        idxs = [indices]
    elif isinstance(indices, (list, tuple)):
        idxs = list(indices)
    else:
        raise StructureError(f"{what} 必须是整数或整数列表，收到 {type(indices).__name__}")
    out = []
    for i in idxs:
        if not isinstance(i, int) or isinstance(i, bool):
            raise StructureError(f"{what} 必须是整数，收到 {i!r}")
        j = i - 1
        if j < 0 or j >= natoms:
            raise StructureError(
                f"{what} {i} 越界：当前结构只有 {natoms} 个原子（1~{natoms}）"
            )
        out.append(j)
    return out


def _regroup(st: Structure, atom_species: list[str], fracs: list[list[float]],
             flags: list[list[bool]] | None, velocities: list[list[float]] | None) -> Structure:
    """按"首次出现顺序"重新分组，保证同元素原子连续（VASP 的硬要求）。

    【因】VASP 按 POSCAR 第 6 行的元素顺序读 POTCAR；同元素不连续时它不会报错，
        但会把后面的原子当成前一种元素 → 算出来的体系是错的
    【坑】只在"换元素/加原子/删原子"后重排；纯扩胞/应变不需要
    """
    order: list[str] = []
    for s in atom_species:
        if s not in order:
            order.append(s)
    buckets: dict[str, list[int]] = {s: [] for s in order}
    for i, s in enumerate(atom_species):
        buckets[s].append(i)
    new_order = [i for s in order for i in buckets[s]]
    new_counts = [len(buckets[s]) for s in order]

    st2 = Structure(
        comment=st.comment,
        scale_raw=1.0,
        lattice_raw=[list(r) for r in st.lattice],
        species=order,
        counts=new_counts,
        coords=[],
        coord_mode=st.coord_mode,
        selective=st.selective,
        flags=[flags[i] for i in new_order] if flags is not None else None,
        velocities=[velocities[i] for i in new_order] if velocities is not None else None,
        tail=list(st.tail),
    )
    st2.set_frac([fracs[i] for i in new_order])
    return st2


def op_supercell(st: Structure, scaling: list[int]) -> tuple[Structure, str]:
    """扩胞：scaling = [nx, ny, nz]，正整数。"""
    if not (isinstance(scaling, (list, tuple)) and len(scaling) == 3):
        raise StructureError(f"supercell 需要 scaling=[nx,ny,nz]，收到 {scaling!r}")
    ns = []
    for x in scaling:
        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            raise StructureError(f"supercell 的缩放必须是正整数，收到 {x!r}")
        ns.append(x)
    if ns == [1, 1, 1]:
        return st, "扩胞 1x1x1（无变化）"

    sp = st.atom_species()
    fracs = st.frac()
    flags = st.flags
    vels = st.velocities

    new_lat_raw = [[ns[i] * x for x in st.lattice[i]] for i in range(3)]
    new_frac: list[list[float]] = []
    new_sp: list[str] = []
    new_flags: list[list[bool]] | None = [] if flags is not None else None
    new_vels: list[list[float]] | None = [] if vels is not None else None

    # 先按元素分组，再在每个元素内部按 (i,j,k) 铺开，保证同元素连续。
    # 【坑】必须对物种"去重后"再遍历：POSCAR 里元素顺序可能是 Mo S Mo S（同元素出现多次），
    #      若直接 for sym in st.species，Mo 会被铺两遍、S 也被铺两遍，
    #      坐标数变成应有的 2 倍而 counts 只乘一次 → 488 号错误"坐标数量与计数不一致"。
    unique_species: list[str] = []
    for s in (st.species or []):
        if s not in unique_species:
            unique_species.append(s)
    for sym in unique_species:
        base = [i for i, s in enumerate(sp) if s == sym]
        for i in base:
            for a in range(ns[0]):
                for b in range(ns[1]):
                    for c in range(ns[2]):
                        new_frac.append([
                            (fracs[i][0] + a) / ns[0],
                            (fracs[i][1] + b) / ns[1],
                            (fracs[i][2] + c) / ns[2],
                        ])
                        new_sp.append(sym)
                        if new_flags is not None:
                            new_flags.append(list(flags[i]))  # type: ignore[index]
                        if new_vels is not None:
                            new_vels.append(list(vels[i]))   # type: ignore[index]

    st2 = Structure(
        comment=st.comment,
        scale_raw=1.0,
        lattice_raw=new_lat_raw,
        species=list(st.species or []),
        counts=[n * (ns[0] * ns[1] * ns[2]) for n in st.counts],
        coords=[],
        coord_mode=st.coord_mode,
        selective=st.selective,
        flags=new_flags,
        velocities=new_vels,
        tail=list(st.tail),
    )
    st2.set_frac(new_frac)
    return st2, f"扩胞 {ns[0]}x{ns[1]}x{ns[2]}：{st.natoms} → {st2.natoms} 原子"


def op_substitute(st: Structure, index: int, species: str) -> tuple[Structure, str]:
    """把第 index 个原子（1 基）换成新元素。"""
    if not _ELEMENT_RE.match(species or ""):
        raise StructureError(f"元素符号不合法：{species!r}")
    idxs = _check_indices(index, st.natoms, "substitute 的 index")
    sp = st.atom_species()
    fracs = st.frac()
    i = idxs[0]
    old = sp[i]
    if old == species:
        return st, f"第 {index} 个原子本来就是 {species}（无变化）"
    sp[i] = species
    st2 = _regroup(st, sp, fracs, st.flags, st.velocities)
    return st2, f"把第 {index} 个原子 {old} → {species}（已按首次出现顺序重新分组）"


def op_substitute_all(st: Structure, mapping: dict[str, str]) -> tuple[Structure, str]:
    """把某种元素**全部**换成另一种（掺杂/合金化最常见的说法："把所有 Cu 换成 Ni"）。

    【因】逐个原子改容易漏，而且改完还要保证同元素连续；
         op_substitute 是按 1 基索引改单个，这里按元素批量改，一次到位。
    """
    if not isinstance(mapping, dict) or not mapping:
        raise StructureError("substitute_all 需要 mapping，如 {\"Cu\": \"Ni\"}")
    norm: dict[str, str] = {}
    for k, v in mapping.items():
        if not _ELEMENT_RE.match(str(k)) or not _ELEMENT_RE.match(str(v)):
            raise StructureError(f"元素符号不合法：{k!r} → {v!r}")
        norm[str(k)] = str(v)
    sp = st.atom_species()
    present = set(sp)
    unknown = [k for k in norm if k not in present]
    if unknown:
        raise StructureError(
            f"结构里没有这些元素：{unknown}；当前结构元素：{sorted(present)}"
        )
    new_sp = [norm.get(s, s) for s in sp]
    changed = sum(1 for a, b in zip(sp, new_sp) if a != b)
    if changed == 0:
        return st, "替换前后元素相同（无变化）"
    st2 = _regroup(st, new_sp, st.frac(), st.flags, st.velocities)
    pairs = "、".join(f"{k}→{v}" for k, v in norm.items())
    return st2, f"批量替换 {pairs}，共 {changed} 个原子（已重新分组）"


def op_vacancy(st: Structure, indices: int | list[int]) -> tuple[Structure, str]:
    """删除一个或多个原子（造空位）。"""
    idxs = _check_indices(indices, st.natoms, "vacancy/remove 的索引")
    idxs = sorted(set(idxs))
    sp = st.atom_species()
    fracs = st.frac()
    keep = [i for i in range(st.natoms) if i not in idxs]
    sp2 = [sp[i] for i in keep]
    fr2 = [fracs[i] for i in keep]
    fl2 = [st.flags[i] for i in keep] if st.flags is not None else None
    vl2 = [st.velocities[i] for i in keep] if st.velocities is not None else None
    if not keep:
        raise StructureError("删除后没有原子了，拒绝生成空结构")
    st2 = _regroup(st, sp2, fr2, fl2, vl2)
    return st2, f"删除原子 {[i + 1 for i in idxs]}：{st.natoms} → {st2.natoms} 原子"


def op_add_atom(st: Structure, frac: list[float] | None = None,
                cart: list[float] | None = None, species: str = "H",
                fixed: bool = False) -> tuple[Structure, str]:
    """加一个原子。位置给分数坐标或笛卡尔坐标二选一。"""
    if not _ELEMENT_RE.match(species or ""):
        raise StructureError(f"元素符号不合法：{species!r}")
    if (frac is None) == (cart is None):
        raise StructureError("add_atom 必须给 frac 或 cart 之一（不能都给/都不给）")
    if frac is not None:
        if len(frac) != 3:
            raise StructureError(f"frac 需要 3 个数，收到 {frac!r}")
        f = [float(x) for x in frac]
    else:
        if len(cart) != 3:  # type: ignore[arg-type]
            raise StructureError(f"cart 需要 3 个数，收到 {cart!r}")
        f = vec_mat([float(x) for x in cart], inv3(st.lattice))  # type: ignore[arg-type]

    sp = st.atom_species() + [species]
    fracs = st.frac() + [f]
    flags = None
    if st.selective:
        flags = [list(x) for x in (st.flags or [])] + [[fixed, fixed, fixed]]
    vels = [list(v) for v in st.velocities] + [[0.0, 0.0, 0.0]] if st.velocities is not None else None
    st2 = _regroup(st, sp, fracs, flags, vels)
    pos = "分数 " + " ".join(_fmt(x) for x in f)
    return st2, f"加原子 {species}（{pos}）：{st.natoms} → {st2.natoms} 原子"


def op_strain(st: Structure, matrix: list[list[float]], mode: str = "delta") -> tuple[Structure, str]:
    """加应变/形变。

    mode="delta"（默认）：matrix 是"增量"，0.02 表示 +2%，内部 F = I + matrix；
    mode="absolute"：matrix 直接就是形变梯度 F（1.02 表示 +2%）。
    新晶格 L' = L @ F^T（行向量约定下，等价于每个晶格矢量左乘 F）。
    """
    if not (isinstance(matrix, list) and len(matrix) == 3
            and all(isinstance(r, (list, tuple)) and len(r) == 3 for r in matrix)):
        raise StructureError(f"strain 需要 3x3 矩阵，收到 {matrix!r}")
    M = [[float(x) for x in row] for row in matrix]
    if mode not in ("delta", "absolute"):
        raise StructureError(f"strain.mode 只能是 delta/absolute，收到 {mode!r}")
    F = [[M[i][j] + (1.0 if i == j else 0.0) for j in range(3)] for i in range(3)] \
        if mode == "delta" else M
    base = st.lattice  # 用 Å 基（scale 已乘进去），新结构统一 scale=1.0
    # L' = L @ F^T，即 L'[i][j] = sum_k L[i][k] * F[j][k]
    new_raw = [[sum(base[i][k] * F[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
    st2 = Structure(
        comment=st.comment, scale_raw=1.0, lattice_raw=new_raw,
        species=st.species, counts=list(st.counts),
        coords=[], coord_mode=st.coord_mode, selective=st.selective,
        flags=[list(f) for f in st.flags] if st.flags is not None else None,
        velocities=[list(v) for v in st.velocities] if st.velocities is not None else None,
        tail=list(st.tail),
    )
    st2.set_frac(st.frac())
    tag = "增量" if mode == "delta" else "绝对"
    return st2, f"应变（{tag}，{_fmt(F[0][0])}/{_fmt(F[1][1])}/{_fmt(F[2][2])} 对角）：体积 {st.volume():.3f} → {st2.volume():.3f} Å³"


def op_vacuum(st: Structure, axis: int = 2, thickness: float = 15.0,
              place: str = "bottom") -> tuple[Structure, str]:
    """沿某个晶格矢量方向加真空层（适合 slab）。

    做法：把该方向晶格矢量拉长到"原子在该方向的跨度 + thickness"，
    再把原子整体平移到从 0 开始（bottom）或居中（center）。
    【坑】对斜晶胞（a·c ≠ 0）而言，"沿矢量投影"只是近似；六方/正交胞是精确的
    【替】严格做法需要重新构造正交盒子，会改变晶面取向，不在本工具范围
    """
    if axis not in (0, 1, 2):
        raise StructureError(f"vacuum 的 axis 只能是 0/1/2，收到 {axis!r}")
    if thickness <= 0:
        raise StructureError("vacuum 的厚度必须 > 0")
    if place not in ("bottom", "center"):
        raise StructureError("vacuum.place 只能是 bottom/center")

    L = st.lattice
    c = L[axis]
    clen = vec_len(c)
    if clen < 1e-8:
        raise StructureError("该方向晶格矢量长度为 0")
    n = vec_scale(c, 1.0 / clen)
    cart = st.cart()
    proj = [sum(p[i] * n[i] for i in range(3)) for p in cart]
    lo, hi = min(proj), max(proj)
    extent = hi - lo
    gap = clen - extent
    # 已经是"至少这么厚"就保持原样，不要为了凑数把用户的大盒子压小。
    # 【坑】只按 thickness 重设长度，会把原本 20 Å 的真空压到 15 Å，原子位置整体平移，
    #      用户会以为"只是加了真空"，实际晶格变小了。
    if gap >= thickness - 1e-9:
        return st, f"沿第 {axis + 1} 个矢量已有真空层 {gap:.2f} Å ≥ 要求 {thickness:g} Å（保持不变）"
    new_len = extent + thickness
    factor = new_len / clen

    base = [list(r) for r in st.lattice]   # Å 基，新结构统一 scale=1.0
    new_raw = [row[:] for row in base]
    new_raw[axis] = vec_scale(new_raw[axis], factor)
    L2 = [row[:] for row in new_raw]
    # 新矢量方向不变；重算投影，平移使最低点落在 0（或居中）
    n2 = vec_scale(L2[axis], 1.0 / vec_len(L2[axis]))
    proj2 = [sum(p[i] * n2[i] for i in range(3)) for p in cart]
    lo2, hi2 = min(proj2), max(proj2)
    if place == "bottom":
        shift = -lo2
    else:
        shift = (new_len - (hi2 - lo2)) / 2.0 - lo2
    cart2 = [vec_add(p, vec_scale(n2, shift)) for p in cart]

    st2 = Structure(
        comment=st.comment, scale_raw=1.0, lattice_raw=new_raw,
        species=st.species, counts=list(st.counts),
        coords=[], coord_mode=st.coord_mode, selective=st.selective,
        flags=[list(f) for f in st.flags] if st.flags is not None else None,
        velocities=[list(v) for v in st.velocities] if st.velocities is not None else None,
        tail=list(st.tail),
    )
    st2.coord_mode = "cartesian"
    st2.coords = cart2
    return st2, f"沿第 {axis + 1} 个矢量加真空 {thickness:g} Å（{place}）：长度 {clen:.3f} → {vec_len(L2[axis]):.3f} Å"


def op_fix_atoms(st: Structure, indices: int | list[int], axes: str = "123",
                 value: bool = True) -> tuple[Structure, str]:
    """固定指定原子在某些方向上（Selective dynamics）。

    【坑】VASP 的 T/F 含义是反的：T = 允许动（relax），F = 固定。
        所以"固定"要写 F。参数 value 的语义是"是否固定"（True=固定），
        内部写 flags=False；写反了表面会算成"原子乱跑"，能量对不上。
    """
    if not axes or any(ch not in "123" for ch in axes):
        raise StructureError(f"axes 只能由 1/2/3 组成，例如 '123'（三个方向）或 '12'（前两个方向），收到 {axes!r}")
    # 兼容旧写法/模型习惯：'111' 这种"同一个方向重复三次"按"三个方向全固定"处理。
    # 真正只固定 x 时应写 '1'，不会写成 '111'；所以这样解释不会误伤正常输入。
    if len(axes) > 1 and len(set(axes)) == 1:
        axes = "123"
    idxs = _check_indices(indices, st.natoms, "fix_atoms 的索引")
    fracs = st.frac()
    flags = [list(f) for f in st.flags] if st.flags is not None else [[True, True, True] for _ in range(st.natoms)]
    for i in idxs:
        for ax in axes:
            # value=True 表示"固定"，POSCAR 里写 F（允许移动=T，固定=F）
            flags[i][int(ax) - 1] = not value
    st2 = Structure(
        comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice],
        species=st.species, counts=list(st.counts), coords=[],
        coord_mode=st.coord_mode, selective=True, flags=flags,
        velocities=[list(v) for v in st.velocities] if st.velocities is not None else None,
        tail=list(st.tail),
    )
    st2.set_frac(fracs)
    verb = "固定" if value else "放开"
    return st2, f"{verb}原子 {[i + 1 for i in idxs]}（方向 {axes}），已启用 Selective dynamics"


def op_reorder_species(st: Structure, order: list[str]) -> tuple[Structure, str]:
    """按给定元素顺序重排原子（为了和 POTCAR 顺序一致）。"""
    sp = st.atom_species()
    present = [s for s in dict.fromkeys(sp)]
    if sorted(order) != sorted(present):
        raise StructureError(
            f"reorder_species 的 order 必须恰好覆盖现有元素。\n"
            f"现有：{present}\n收到：{order}"
        )
    idxs = [i for sym in order for i, s in enumerate(sp) if s == sym]
    fracs = st.frac()
    st2 = _regroup(st, [sp[i] for i in idxs], [fracs[i] for i in idxs],
                   [st.flags[i] for i in idxs] if st.flags is not None else None,
                   [st.velocities[i] for i in idxs] if st.velocities is not None else None)
    return st2, f"按 {order} 重排元素（原子编号全部变化）"


def op_translate(st: Structure, vector: list[float], mode: str = "direct") -> tuple[Structure, str]:
    """整体平移。mode=direct 时 vector 是分数坐标增量，cart 时是 Å。"""
    if not (isinstance(vector, (list, tuple)) and len(vector) == 3):
        raise StructureError(f"translate 需要 3 个数，收到 {vector!r}")
    v = [float(x) for x in vector]
    fracs = st.frac()
    if mode == "cart":
        dv = vec_mat(v, inv3(st.lattice))
    elif mode == "direct":
        dv = v
    else:
        raise StructureError("translate.mode 只能是 direct/cart")
    st2 = Structure(
        comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice],
        species=st.species, counts=list(st.counts), coords=[],
        coord_mode=st.coord_mode, selective=st.selective,
        flags=[list(f) for f in st.flags] if st.flags is not None else None,
        velocities=[list(vv) for vv in st.velocities] if st.velocities is not None else None,
        tail=list(st.tail),
    )
    st2.set_frac([vec_add(f, dv) for f in fracs])
    return st2, f"整体平移 {v}（{mode}）"


def op_wrap(st: Structure) -> tuple[Structure, str]:
    """把所有分数坐标折回 [0, 1)。"""
    st2 = Structure(
        comment=st.comment, scale_raw=1.0, lattice_raw=[list(r) for r in st.lattice],
        species=st.species, counts=list(st.counts), coords=[],
        coord_mode=st.coord_mode, selective=st.selective,
        flags=[list(f) for f in st.flags] if st.flags is not None else None,
        velocities=[list(v) for v in st.velocities] if st.velocities is not None else None,
        tail=list(st.tail),
    )
    st2.set_frac([[x % 1.0 for x in f] for f in st.frac()])
    return st2, "所有原子折回 [0,1) 分数坐标"


# 解析流程（给读代码的人）：
#   第 1 行 comment → 第 2 行 scale → 3~5 行晶格 → 第 6 行元素（或原子数）
#   → （可选）Selective dynamics → 坐标模式 → natoms 行坐标(+T/F)
#   → （可选）natoms 行速度 → 尾部原样保留
