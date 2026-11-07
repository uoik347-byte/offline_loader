# Build the egg tool on 64-bit Windows. Run from the repository root.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller --noconfirm --clean egg_hatch.spec
if (-not (Test-Path ".\dist\AolaEggHatch.exe")) {
    throw "PyInstaller did not write dist\AolaEggHatch.exe"
}
$bytes = [System.IO.File]::ReadAllBytes(".\dist\AolaEggHatch.exe")
if ($bytes.Length -lt 2 -or $bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) {
    throw "dist\AolaEggHatch.exe is not a PE executable"
}
Write-Host "Built dist\AolaEggHatch.exe"
