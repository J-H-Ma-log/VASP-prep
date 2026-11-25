# Git 与 PyCharm 新手手册（本项目专用）

> 目标：不需要懂原理，照着做就能"每次改完存一版、随时退回上一版"。
> 这份手册里的命令都可以直接复制运行；路径已换成你这台机器。

---

## 0. 三句话理解 Git

1. **Git 是"存档"**：每次 `commit` 给当前所有文件拍一张快照，附一句说明。
2. **存档在本地**：不联网也能用；想备份到网上（Gitee/GitHub）才需要 `push`。
3. **`tag` 是给某张快照起名**：比如把 v4 的提交命名为 `v4.0.0`，以后一句 `git show v4.0.0` 就能回到那天。

---

## 1. 已经给你建好的仓库

```
位置：C:\Users\28476\Desktop\Deepseek Harness\科研\vasp-prep
分支：main
标签：v4.0.0（当前代码）
```

第一次使用时，在 PowerShell 里：

```powershell
cd "C:\Users\28476\Desktop\Deepseek Harness\科研\vasp-prep"
git log --oneline        # 看历史（现在只有 1~2 条）
git status               # 看"有没有还没存的东西"
git tag                  # 看有哪些版本名
```

---

## 2. 最常用的 8 条命令

| 想干什么 | 命令 | 说明 |
|---|---|---|
| 看当前状态 | `git status` | 哪些文件改了、哪些新增了 |
| 看改了什么 | `git diff` | 逐行差异；`git diff --stat` 只看统计 |
| 存一版 | `git add -A` 然后 `git commit -m "说明"` | 两步：先挑文件，再存档 |
| 看历史 | `git log --oneline --graph` | 一行一条，带分支图 |
| 看某次改了什么 | `git show <提交号>` | 提交号从 `git log` 复制前 7 位 |
| 撤销**未提交**的修改 | `git restore <文件>` | 危险：改的内容直接没了 |
| 撤销**已提交**的一次 | `git revert <提交号>` | 安全：生成一次"反向提交" |
| 回到某个版本看看 | `git switch --detach v4.0.0` | 只读查看，改完用 `git switch main` 回来 |

---

## 3. 日常三步（90% 的时间只用这个）

```powershell
# ① 改完代码后，先看自己动了什么
git status
git diff --stat

# ② 全部存下（.gitignore 已经排除了 runs/、batches/、materials/、缓存）
git add -A
git commit -m "批量页：新增 IVDW 参数扫描"

# ③ 确认存好了
git log --oneline -3
```

> 提交说明写"做了什么"，别写"update"。
> 例：`修复元素行重复导致扩胞翻倍`、`数据库页增加导入 CIF`。

---

## 4. 想退回上一版

### 情况 A：改了但**还没提交**，想全部放弃

```powershell
git restore .            # 放弃所有未提交修改（新增文件用 git clean -fd 清掉）
```

### 情况 B：已经提交了，但发现这次改坏了

```powershell
git log --oneline                 # 找到坏的那次提交号，例如 a1b2c3d
git revert a1b2c3d                # 生成一次反向提交，历史保留、代码回退
```

### 情况 C：只想看某天的代码长什么样

```powershell
git show v4.0.0:web/index.html    # 直接看某个版本里的某个文件
git switch --detach v4.0.0        # 整个目录切到那个版本（只读查看）
git switch main                   # 看完切回来
```

⚠️ `git reset --hard` 会**丢掉**之后的提交，新手不要用；要回退请用 `revert`。

---

## 5. 打标签（版本号）

```powershell
git tag -a v4.0.0 -m "批量 + 数据库"     # 给当前提交打标签
git tag                                  # 列出所有标签
git show v4.0.0                          # 看这个版本的信息
```

以后想发 v4.1：

```powershell
git add -A
git commit -m "v4.1：修复 xxx"
git tag -a v4.1.0 -m "修复 xxx"
```

---

## 6. 备份到 Gitee（可选，但强烈建议）

1. 在 Gitee 新建一个**空仓库**（不要勾选"初始化 README"），拿到地址，例如
   `https://gitee.com/你的用户名/vasp-prep.git`；
2. 在本项目里执行：

```powershell
git remote add origin https://gitee.com/你的用户名/vasp-prep.git
git push -u origin main
git push origin --tags
```

次日起只需 `git push`。
如果提示登录：Gitee 用"用户名 + 私人令牌（私人令牌在 设置→安全设置→私人令牌 生成）"。

---

## 7. PyCharm 能用吗？能，而且很适合

### 7.1 打开项目

`File → Open` → 选 `vasp-prep` 文件夹（不是里面的 `vasp_core`）。

### 7.2 配解释器（关键一步）

`File → Settings → Project: vasp-prep → Python Interpreter → Add Interpreter → System Interpreter`

| 用途 | 解释器路径 | 能力 |
|---|---|---|
| 日常开发/网页/数据库 | `C:\Users\28476\Desktop\Deepseek Harness\科研\chgnet_env\python\python.exe` | 有 pymatgen/ase，能读 CIF、跑网页 |
| 只跑单元测试 | `D:\Scientific Exploration Tools\Python\python.exe` | 纯标准库，163 项测试都能过 |

> 说明：`chgnet_env` 是"嵌入式 Python"，没有 venv，但 PyCharm 可以把它当 System Interpreter 用。

### 7.3 运行/调试配置

`Run → Edit Configurations → + → Python`

- **网页**：Script = `webui_launcher.py`，Working directory = 项目根目录；
- **统一入口**：Module name = `vasp_core`，Parameters = `webui`（或 `batch -h` 看帮助）；
- **单元测试**：`Run → Edit Configurations → + → Python tests → Unittests`，
  Target 选 `tests` 目录，解释器选系统 Python 3.14。

### 7.4 用 PyCharm 做 Git

- 右侧/底部 **Git** 工具窗口：看改动、写提交说明、点 Commit；
- **Log** 标签：看历史、右键某次提交 → `Revert Commit`（安全回退）；
- 左下 **Git → Branches / Tags**：切分支、看标签；
- 改了文件左侧会出现蓝色竖条，右键 `Git → Rollback` 可撤销未提交修改。

### 7.5 三个坑

1. 打开项目后如果满屏红：解释器没配对，回 7.2；
2. 运行网页时**工作目录**必须是项目根（否则找不到 `web/index.html`、`vasp-prep.toml`）；
3. 不要提交 `runs/`、`batches/`、`materials/`——`.gitignore` 已排除；如果 PyCharm 仍显示，右键 `Git → Add to .gitignore`。

---

## 8. 为什么 v2 / v3 没有独立快照

当时的代码是**在同一个文件夹里连续改**的，没有存过档，事后无法凭空还原；
所以 Git 历史从 v4 开始，之前的版本用 `CHANGELOG.md` 记录"每一版做了什么、修了什么"。
从现在起每改一次就 `commit` 一次，就不会再出现"想回退却没有存档"的情况。
