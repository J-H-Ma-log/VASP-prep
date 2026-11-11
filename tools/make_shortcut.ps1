# tools/make_shortcut.ps1
# Create a Desktop shortcut that launches the local web UI.
#
# ASCII-only on purpose:
#   Windows PowerShell 5.1 reads .ps1 files as ANSI unless they have a
#   UTF-8 BOM; keeping this file ASCII means it works everywhere.
#
# Parameters:
#   -DestinationFolder  where to put the .lnk (default: user's Desktop)
#   -Name               shortcut file name without .lnk (default: VASP-Prep)

param(
    [string]$DestinationFolder = [Environment]::GetFolderPath('Desktop'),
    [string]$Name = ''
)

$ErrorActionPreference = 'Stop'

# Shortcut name: prefer tools\shortcut_name.txt (UTF-8, may contain Chinese,
# read explicitly as UTF-8 so the console code page cannot garble it).
if (-not $Name) {
    $nameFile = Join-Path $PSScriptRoot 'shortcut_name.txt'
    if (Test-Path $nameFile) {
        $Name = ([System.IO.File]::ReadAllText($nameFile, [System.Text.Encoding]::UTF8)).Trim()
    }
    if (-not $Name) { $Name = 'VASP-Prep' }
}

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'start_webui.bat'
if (-not (Test-Path $target)) {
    Write-Error "launcher not found: $target"
}

if (-not (Test-Path $DestinationFolder)) {
    New-Item -ItemType Directory -Path $DestinationFolder | Out-Null
}

$lnkPath = Join-Path $DestinationFolder ($Name + '.lnk')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$lnk.Description = 'VASP preprocessing web UI (runs locally)'
$lnk.WindowStyle = 1
$lnk.Save()

Write-Host "Created shortcut: $lnkPath"
Write-Host "Target          : $target"
