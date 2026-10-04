<#
.SYNOPSIS  Build the ScreenTime Windows bundle, installer and portable zip.
.NOTES     Needs MSYS2 (C:\msys64 or -Msys2Root) and Inno Setup 6 (ISCC.exe on PATH or -Iscc).
           Nothing here is needed by END USERS; they only run the produced setup .exe.
#>
param(
  [string]$Msys2Root = "C:\msys64",
  [string]$Iscc = "ISCC.exe",
  [switch]$VerifyLock
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$version = (Select-String -Path "$repo\pyproject.toml" -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$bash = Join-Path $Msys2Root "usr\bin\bash.exe"
if (-not (Test-Path $bash)) { throw "MSYS2 not found at $Msys2Root" }

$repoMsys = ($repo -replace '\\','/') -replace '^([A-Za-z]):','/$1'
$flag = if ($VerifyLock) { "--verify-lock" } else { "" }
$env:MSYSTEM = "MINGW64"; $env:CHERE_INVOKING = "1"
& $bash -lc "cd '$repoMsys' && packaging/windows/bundle.sh $flag"
if ($LASTEXITCODE) { throw "bundle.sh failed ($LASTEXITCODE)" }

& "$repo\packaging\windows\smoke.ps1" -Mode Bundle -Bundle "$repo\dist\ScreenTime"
if ($LASTEXITCODE) { throw "bundle smoke test failed" }

& $Iscc "/DAppVersion=$version" "/DBundleDir=$repo\dist\ScreenTime" "$repo\packaging\windows\installer.iss"
if ($LASTEXITCODE) { throw "Inno Setup failed" }

$zip = "$repo\dist\ScreenTime-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$repo\dist\ScreenTime" -DestinationPath $zip
foreach ($f in @("$repo\dist\ScreenTime-$version-setup.exe", $zip)) {
  if (-not (Test-Path $f)) { throw "expected artifact missing: $f" }
  Write-Host ("{0}  {1:N1} MB" -f $f, ((Get-Item $f).Length / 1MB))
}
