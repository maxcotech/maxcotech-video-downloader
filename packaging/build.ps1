# Builds dist\MaxcotechVideoDownloader-Setup-<version>.exe
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Needs Python 3.10+ and Inno Setup 6 (winget install JRSoftware.InnoSetup).
# FFmpeg and Deno are downloaded (checksum-verified) on the first build; delete
# %LOCALAPPDATA%\ZBD-build\tools to fetch newer ones.
# Intermediate files go to %LOCALAPPDATA%\ZBD-build (a short path, so Windows'
# 260-character path limit is never a problem).

$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $env:LOCALAPPDATA "ZBD-build"
$Venv = Join-Path $Build "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Name = "MaxcotechVideoDownloader"

$Version = (Select-String -Path "$Project\zoom_downloader.py" -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building Maxcotech Video Downloader $Version" -ForegroundColor Cyan

# 1. Build environment ("default" extra = YouTube challenge solver yt-dlp-ejs)
if (-not (Test-Path $Py)) {
    New-Item -ItemType Directory -Force $Build | Out-Null
    py -3 -m venv $Venv
}
& $Py -m pip install --disable-pip-version-check -q -U pyinstaller "yt-dlp[default]" requests pillow
if ($LASTEXITCODE) { throw "pip install failed" }

# 2. Helper programs: FFmpeg (LGPL) + Deno
$Tools = Join-Path $Build "tools"
& $Py -I "$PSScriptRoot\fetch_tools.py" $Tools
if ($LASTEXITCODE) { throw "fetching FFmpeg/Deno failed" }

# 3. Icon
$Icon = Join-Path $Build "app.ico"
& $Py "$PSScriptRoot\make_icon.py" $Icon
if ($LASTEXITCODE) { throw "icon failed" }

# 4. App -> folder with MaxcotechVideoDownloader.exe
$Work = Join-Path $Build "work"
$Dist = Join-Path $Build "dist"
Remove-Item -Recurse -Force $Work, $Dist -ErrorAction SilentlyContinue
& $Py -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name $Name `
    --icon $Icon --add-data "$Icon;." `
    --add-data "$Tools\ffmpeg;tools\ffmpeg" `
    --add-data "$Tools\deno;tools\deno" `
    --copy-metadata yt-dlp `
    --collect-all yt_dlp_ejs `
    --distpath $Dist --workpath $Work --specpath $Build `
    "$Project\zoom_downloader.py"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# 5. Installer
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }

$Out = Join-Path $Project "dist"
New-Item -ItemType Directory -Force $Out | Out-Null
& $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$Dist\$Name" "/DOutDir=$Out" `
    "/DIconFile=$Icon" "/DGuideFile=$PSScriptRoot\How to use.txt" `
    "/DNoticesFile=$PSScriptRoot\Third-party notices.txt" "$PSScriptRoot\installer.iss"
if ($LASTEXITCODE) { throw "Inno Setup failed" }

$Setup = Join-Path $Out "$Name-Setup-$Version.exe"
Write-Host ("Done: {0}  ({1:N1} MB)" -f $Setup, ((Get-Item $Setup).Length / 1MB)) -ForegroundColor Green
