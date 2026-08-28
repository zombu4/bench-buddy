<#
.SYNOPSIS
    Builds Bench Buddy for Windows: PyInstaller one-folder bundle
    plus an Inno Setup installer.

.DESCRIPTION
    Produces
        build/dist/bench-buddy/            the self-contained application folder
        build/installer/bench-buddy-<ver>-windows-x64-setup.exe

    Everything the app needs -- Python, Qt, numpy, Pillow and the four bundled
    fonts -- is inside the folder.  The target machine needs nothing installed.

    The version is read from app/__init__.py; it is never typed in here.

.PARAMETER SkipInstaller
    Build the PyInstaller folder only and stop.  Useful when Inno Setup is not
    available, or when iterating on the frozen app itself.

.PARAMETER Python
    Interpreter to build with.  Defaults to whatever `python` resolves to.

.EXAMPLE
    .\packaging\build_windows.ps1
    .\packaging\build_windows.ps1 -SkipInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root         = Split-Path -Parent $PackagingDir
$BuildDir     = Join-Path $Root 'build'
$DistDir      = Join-Path $BuildDir 'dist'
$WorkDir      = Join-Path $BuildDir 'work'
$InstallerDir = Join-Path $BuildDir 'installer'
$AppDir       = Join-Path $DistDir 'bench-buddy'

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------- version, single source
$initPath = Join-Path $Root 'app\__init__.py'
if (-not (Test-Path $initPath)) { Fail "cannot find $initPath" }
$match = Select-String -Path $initPath -Pattern '^__version__\s*=\s*["'']([^"'']+)["'']'
if (-not $match) { Fail 'app/__init__.py does not define __version__' }
$Version = $match.Matches[0].Groups[1].Value

Write-Host ''
Write-Host 'Bench Buddy - Windows build' -ForegroundColor White
Write-Host "  version : $Version"
Write-Host "  root    : $Root"
Write-Host ''

# ------------------------------------------------------------------ toolchain
Write-Step 'Checking build tools'
try { $pyVersion = & $Python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' }
catch { Fail "cannot run '$Python'. Install Python 3.11+ and put it on PATH." }
Write-Host "    python       $pyVersion"

& $Python -c 'import PyInstaller' 2>$null
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller is not installed. Run: $Python -m pip install pyinstaller" }
$piVersion = & $Python -m PyInstaller --version
Write-Host "    pyinstaller  $piVersion"

foreach ($module in 'PySide6', 'numpy', 'PIL') {
    & $Python -c "import $module" 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "$module is not installed. Run: $Python -m pip install -r requirements.txt" }
}
Write-Host '    PySide6, numpy, Pillow present'

# ---------------------------------------------------------------------- icons
$icon = Join-Path $PackagingDir 'icons\icon.ico'
if (-not (Test-Path $icon)) {
    Write-Step 'Generating icons'
    & $Python (Join-Path $PackagingDir 'make_icons.py')
    if ($LASTEXITCODE -ne 0) { Fail 'icon generation failed' }
}

# ------------------------------------------------------------------- freeze it
Write-Step 'Running PyInstaller (one-folder)'
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
& $Python -m PyInstaller --noconfirm --clean `
    --distpath $DistDir --workpath $WorkDir `
    (Join-Path $PackagingDir 'bench-buddy.spec')
if ($LASTEXITCODE -ne 0) { Fail 'PyInstaller failed' }

$exe = Join-Path $AppDir 'bench-buddy.exe'
if (-not (Test-Path $exe)) { Fail "PyInstaller did not produce $exe" }

# ---------------------------------------------------- payload sanity, not faith
Write-Step 'Verifying the bundle carries its dependencies'
$fontDir = Join-Path $AppDir '_internal\app\ui\fonts'
$required = @(
    'MartianMono-Regular.ttf', 'IBMPlexSans-Regular.ttf',
    'IBMPlexMono-Regular.ttf', 'IBMPlexMono-Medium.ttf',
    'OFL-IBMPlex.txt', 'OFL-MartianMono.txt'
)
foreach ($name in $required) {
    if (-not (Test-Path (Join-Path $fontDir $name))) { Fail "font payload missing: $name" }
}
Write-Host "    fonts        4 TTF + 2 OFL licences at _internal\app\ui\fonts"

$internal = Join-Path $AppDir '_internal'
foreach ($pattern in 'PySide6\Qt6Core.dll', 'PySide6\Qt6Gui.dll', 'PySide6\Qt6Widgets.dll') {
    if (-not (Test-Path (Join-Path $internal $pattern))) { Fail "Qt payload missing: $pattern" }
}
if (-not (Get-ChildItem $internal -Filter 'python3*.dll' -File)) { Fail 'python DLL missing from bundle' }
if (-not (Test-Path (Join-Path $internal 'numpy'))) { Fail 'numpy missing from bundle' }
if (-not (Test-Path (Join-Path $internal 'PIL'))) { Fail 'Pillow missing from bundle' }
Write-Host '    runtime      Qt6 Core/Gui/Widgets, python3x.dll, numpy, PIL'

# The deleted web stack must not have come back in transitively.
foreach ($banned in 'fastapi', 'uvicorn', 'starlette') {
    if (Test-Path (Join-Path $internal $banned)) { Fail "excluded package present in bundle: $banned" }
}
Write-Host '    excluded     no fastapi / uvicorn / starlette'

$sizeMb = [math]::Round(((Get-ChildItem $AppDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "    folder size  $sizeMb MB"

if ($SkipInstaller) {
    Write-Host ''
    Write-Host "Application folder: $AppDir" -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------------ Inno Setup
Write-Step 'Building the installer with Inno Setup'
$isccCandidates = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    $onPath = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($onPath) { $iscc = $onPath.Source }
}
if (-not $iscc) {
    Write-Host ''
    Write-Host 'Inno Setup 6 was not found. Install it with:' -ForegroundColor Yellow
    Write-Host '    winget install --id JRSoftware.InnoSetup' -ForegroundColor Yellow
    Write-Host 'then re-run this script. The application folder is already built at:' -ForegroundColor Yellow
    Write-Host "    $AppDir"
    exit 1
}
Write-Host "    iscc         $iscc"

New-Item -ItemType Directory -Force -Path $InstallerDir | Out-Null
& $iscc `
    "/DAppVersion=$Version" `
    "/DSourceDir=$AppDir" `
    "/DAssetsDir=$PackagingDir" `
    "/O$InstallerDir" `
    (Join-Path $PackagingDir 'windows\installer.iss')
if ($LASTEXITCODE -ne 0) { Fail 'Inno Setup failed' }

$setup = Join-Path $InstallerDir "bench-buddy-$Version-windows-x64-setup.exe"
if (-not (Test-Path $setup)) { Fail "installer not produced at $setup" }
$setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)

Write-Host ''
Write-Host 'Build complete.' -ForegroundColor Green
Write-Host "  application : $AppDir  ($sizeMb MB)"
Write-Host "  installer   : $setup  ($setupMb MB)"
Write-Host ''
Write-Host 'The installer is per-user by default and needs no administrator rights.'
