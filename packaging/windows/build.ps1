<#
.SYNOPSIS
  Build the offline Windows bundle of Pillow Assistant.

.DESCRIPTION
  Produces dist\PillowAssistant\ (and a .zip of it) containing:
    python\         relocatable CPython (NuGet "python" package) + every dependency
    app\            the committed source tree + VERSION.json
    ms-playwright\  Chromium for the browser tool
    cad-runtime\    uv cache with the pinned text-to-cad cadgen runtime (CAD works offline),
                    and apps\pins.json for CAD apps installed on first use
    launcher.pyw, update.json, PillowAssistant.exe, PillowAssistant-console.bat
  Optionally copies a prefetched models\ folder (see prefetch_models.py).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -ModelsDir C:\models
#>
param(
    [string]$PythonVersion = "3.12.10",
    [string]$OutDir = "dist",
    [string]$Version = "",
    [string]$ModelsDir = "",
    [switch]$SkipBrowser,
    [switch]$SkipCad,
    [switch]$SkipZip,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow otherwise

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $exe $($argv -join ' ')" }
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Repo
$Out = Join-Path $Repo $OutDir
$Bundle = Join-Path $Out "PillowAssistant"
$Work = Join-Path $Out "_work"

$Sha = (git rev-parse HEAD).Trim()
$Ref = if ($env:GITHUB_REF_NAME) { $env:GITHUB_REF_NAME } else { (git rev-parse --abbrev-ref HEAD).Trim() }
if (-not $Version) { $Version = (git describe --tags --always).Trim() }

Step "Bundle $Version ($Sha) -> $Bundle"
if (Test-Path $Bundle) { Remove-Item -Recurse -Force $Bundle }
New-Item -ItemType Directory -Force -Path $Bundle, $Work | Out-Null

# 1. Relocatable CPython from NuGet (full stdlib, pip, pythonw.exe, no installer).
Step "Python $PythonVersion"
$Nupkg = Join-Path $Work "python-$PythonVersion.zip"
if (-not (Test-Path $Nupkg)) {
    Invoke-WebRequest "https://www.nuget.org/api/v2/package/python/$PythonVersion" -OutFile $Nupkg
}
$PyExtract = Join-Path $Work "python-$PythonVersion"
if (Test-Path $PyExtract) { Remove-Item -Recurse -Force $PyExtract }
Expand-Archive $Nupkg -DestinationPath $PyExtract
Copy-Item -Recurse (Join-Path $PyExtract "tools") (Join-Path $Bundle "python")
$Py = Join-Path $Bundle "python\python.exe"
if (-not (Test-Path (Join-Path $Bundle "python\pythonw.exe"))) {
    Write-Warning "pythonw.exe missing; the app will start with a console window"
}
$env:PYTHONNOUSERSITE = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
Run $Py @("-E", "-s", "-m", "ensurepip", "--upgrade")
Run $Py @("-E", "-s", "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

# 2. Every runtime dependency (requirements.txt is the superset of all extras).
#    On Windows the PyPI torch wheels are CPU builds, which is what we want.
Step "Dependencies"
Run $Py @("-E", "-s", "-m", "pip", "install", "--no-warn-script-location", "--no-cache-dir",
          "-r", (Join-Path $Repo "requirements.txt"))
& $Py -E -s -m pip check
if ($LASTEXITCODE -ne 0) { Write-Warning "pip check reported conflicts (see above)" }

# 3. Chromium for the Playwright browser tool, stored inside the bundle.
if (-not $SkipBrowser) {
    Step "Chromium (Playwright)"
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Bundle "ms-playwright"
    & $Py -E -s -m playwright install --only-shell chromium
    if ($LASTEXITCODE -ne 0) { Run $Py @("-E", "-s", "-m", "playwright", "install", "chromium") }
}

# 3b. text-to-cad: prewarm the cadgen runtime that the vendored Skills pin, in the
#     same folders the launcher points uv at, so CAD modelling works offline. uv
#     rebuilds the cached environment on its own if the bundle is moved.
if (-not $SkipCad) {
    Step "text-to-cad runtime (cadgen)"
    $Upstream = Get-Content -Raw -Encoding UTF8 (Join-Path $Repo "pillow_assistant\capabilities\skills\text_to_cad\UPSTREAM.json") | ConvertFrom-Json
    $Cad = Join-Path $Bundle "cad-runtime"
    $env:UV_CACHE_DIR = Join-Path $Cad "cache"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Cad "python"
    $env:UV_TOOL_DIR = Join-Path $Cad "tools"
    $env:DO_NOT_TRACK = "1"
    $Uvx = Join-Path $Bundle "python\Scripts\uvx.exe"
    Run $Uvx (@($Upstream.uvx_args) + @("python", "-c", "import build123d, cadgen; print('cadgen ok')"))
    Remove-Item Env:UV_CACHE_DIR, Env:UV_PYTHON_INSTALL_DIR, Env:UV_TOOL_DIR, Env:DO_NOT_TRACK
}

# 3c. CAD apps (FreeCAD, OrcaSlicer, Bambu Studio) install on first use, or come from
#     the separate PillowAssistant-<app>.zip release packs. Pin the release assets
#     (URL + SHA-256) so first-use installs still work through a mirror when the
#     GitHub API is unreachable (mainland China).
if (-not $SkipCad) {
    Step "CAD app pins"
    $Pins = Join-Path $Bundle "cad-runtime\apps\pins.json"
    & $Py -E -s -m pillow_assistant.capabilities.cad_apps pin windows-x64 $Pins
    if ($LASTEXITCODE -ne 0) { Write-Warning "could not pin CAD app releases; first-use installs need the GitHub API" }
}

# 4. Application source: exactly the committed tree, like the updater downloads.
Step "Application source"
$AppZip = Join-Path $Work "app.zip"
Run git @("archive", "--format=zip", "-o", $AppZip, "HEAD")
Expand-Archive $AppZip -DestinationPath (Join-Path $Bundle "app")
Copy-Item (Join-Path $PSScriptRoot "launcher.py") (Join-Path $Bundle "launcher.pyw")
Copy-Item (Join-Path $PSScriptRoot "update.json") (Join-Path $Bundle "update.json")
Copy-Item (Join-Path $PSScriptRoot "PillowAssistant-console.bat") $Bundle
Run $Py @("-E", "-s", (Join-Path $Bundle "launcher.pyw"), "--stamp-version", $Sha, $Ref)

# 5. No-console starter exe, compiled with the .NET Framework C# compiler.
Step "PillowAssistant.exe"
$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$Ico = Join-Path $Bundle "PillowAssistant.ico"
$IconArgs = @()
& $Py -E -s (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $Bundle "app") $Ico
if ($LASTEXITCODE -eq 0 -and (Test-Path $Ico)) { $IconArgs = @("/win32icon:$Ico") }
else { Write-Warning "icon rendering failed; PillowAssistant.exe will use the default icon" }
Run $Csc (@("/nologo", "/target:winexe", "/codepage:65001", "/optimize+") + $IconArgs + @(
           "/out:$(Join-Path $Bundle 'PillowAssistant.exe')",
           "/reference:System.Windows.Forms.dll", "/reference:System.Core.dll",
           (Join-Path $PSScriptRoot "PillowAssistant.cs")))

# 6. Optional offline speech models.
if ($ModelsDir) {
    Step "Models from $ModelsDir"
    Copy-Item -Recurse $ModelsDir (Join-Path $Bundle "models")
}

# 7. Precompile for faster first start, drop caches.
Step "Precompile"
& $Py -E -s -m compileall -q -j 0 (Join-Path $Bundle "python\Lib") (Join-Path $Bundle "app") | Out-Null
Get-ChildItem -Recurse -Directory -Path (Join-Path $Bundle "python") -Filter "tests" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\(numpy|pandas|matplotlib|scipy|sklearn)\\" } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (-not $SkipSelfTest) {
    Step "Self test"
    Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
    Run $Py @("-E", "-s", (Join-Path $Bundle "launcher.pyw"), "--self-test")
}

if (-not $SkipZip) {
    Step "Zip"
    $Zip = Join-Path $Out "PillowAssistant-windows-x64-$Version.zip"
    if (Test-Path $Zip) { Remove-Item -Force $Zip }
    Push-Location $Out
    try { Run 7z @("a", "-tzip", "-mx=7", "-mmt=on", $Zip, "PillowAssistant") } finally { Pop-Location }
    $Hash = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
    "$Hash  $(Split-Path -Leaf $Zip)" | Out-File -Encoding ascii "$Zip.sha256"
    $SizeMB = [math]::Round((Get-Item $Zip).Length / 1MB)
    Step "Done: $Zip ($SizeMB MB)"
}
