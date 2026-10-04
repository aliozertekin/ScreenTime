<#
.SYNOPSIS  Smoke-test a ScreenTime bundle or the installer on a (clean) Windows machine.
  -Mode Bundle     : run from a built bundle folder (no install).
  -Mode Installer  : silently install, exercise daemon + startup task, uninstall, assert removal.
Exit code is non-zero on the first failed assertion.
#>
param(
  [ValidateSet("Bundle","Installer")][string]$Mode = "Bundle",
  [string]$Bundle,
  [string]$Setup,
  [string]$InstallDir = "$env:RUNNER_TEMP\ScreenTimeInstalled"
)
$ErrorActionPreference = "Stop"
function Assert($cond, $msg) { if (-not $cond) { Write-Error "SMOKE FAIL: $msg"; exit 1 } else { Write-Host "ok  - $msg" } }

# Never touch the real profile: private data + config dirs for the whole run.
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("screentime-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force "$tmp\L","$tmp\R" | Out-Null
$env:LOCALAPPDATA = "$tmp\L"; $env:APPDATA = "$tmp\R"

# Start-Process -ArgumentList joins an array with plain spaces WITHOUT quoting, so `-c "import gi; ..."` reached Python
# as `-c import gi; ...` and python saw only `import` ("SyntaxError: Expected one or more names after 'import'").
# Quote each argument the way the Windows C runtime parses a command line.
function ConvertTo-QuotedArg([string]$a) {
  if ($a -ne "" -and $a -notmatch '[\s"]') { return $a }
  $a = $a -replace '(\\*)"', '$1$1\"'      # backslashes before a quote are doubled, the quote is escaped
  $a = $a -replace '(\\+)$', '$1$1'         # trailing backslashes are doubled before the closing quote
  return '"' + $a + '"'
}

function Run-Cli($root, [string[]]$args_, [int]$timeoutSec = 0) {
  $exe = Join-Path $root "bin\screentime-cli.exe"
  $argLine = ($args_ | ForEach-Object { ConvertTo-QuotedArg $_ }) -join " "
  $p = Start-Process -FilePath $exe -ArgumentList $argLine -NoNewWindow -PassThru -RedirectStandardOutput "$tmp\out.txt" -RedirectStandardError "$tmp\err.txt"
  $limit = if ($timeoutSec -gt 0) { $timeoutSec * 1000 } else { -1 }
  if (-not $p.WaitForExit($limit)) { $p.Kill(); $p.WaitForExit() }     # a still-running GUI after $timeoutSec s is a success for the re-run
  # Get-Content -Raw yields $null (not "") for an empty file -- and [string](...) keeps it $null -- while the assert
  # messages below call .Trim() on these, eagerly, even when the assertion passes. A clean run has empty stderr,
  # so wrap in "$( )", which always produces a string.
  return @{ Code = $p.ExitCode; Out = "$(Get-Content "$tmp\out.txt" -Raw)"; Err = "$(Get-Content "$tmp\err.txt" -Raw)" }
}

if ($Mode -eq "Installer") {
  Assert (Test-Path $Setup) "installer exists: $Setup"
  $p = Start-Process $Setup -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/DIR=`"$InstallDir`"" -PassThru
  if (-not $p.WaitForExit(300000)) { $p.Kill(); Assert $false "silent install finished within 5 minutes" }   # never hang the job
  Assert ($p.ExitCode -eq 0) "silent install exits 0 (got $($p.ExitCode))"
  $Bundle = $InstallDir
}

# --- files
foreach ($f in "bin\screentime-daemon.exe","bin\screentime-gui.exe","bin\screentime-cli.exe","screentime.ico","app\screentime\daemon.py") {
  Assert (Test-Path (Join-Path $Bundle $f)) "bundle contains $f"
}

# --- GTK4 / libadwaita / PyGObject import from the bundled interpreter only (no MSYS2 on PATH)
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$r = Run-Cli $Bundle @("-c","import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw; print('gtk', Gtk.get_major_version(), 'adw', Adw.get_major_version())")
Assert ($r.Code -eq 0 -and $r.Out -match "gtk 4") "bundled GTK4 + libadwaita import ($($r.Out.Trim()) $($r.Err.Trim()))"

# --- diagnostics reports Windows backends and leaks no secrets
$r = Run-Cli $Bundle @("-m","screentime.diagnostics")
Assert ($r.Code -eq 0 -and $r.Out -match "OS:\s+Windows") "diagnostics reports OS: Windows"
# Look for the SHAPE of a secret, not for words: the report's own disclaimer says "...recovery keys or credentials".
$secretShapes = @(
  '\b[A-Z2-7]{4}(-[A-Z2-7]{4}){5,}\b',      # grouped Base32 recovery key
  '\b[0-9a-fA-F]{32,}\b',                   # raw key / store id in hex
  '[A-Za-z0-9+/]{40,}={0,2}',                # base64 key material
  'BEGIN [A-Z ]*(KEY|CERTIFICATE)'           # PEM blocks
)
$leaks = $secretShapes | Where-Object { $r.Out -match $_ }
Assert (-not $leaks) "diagnostics output contains no secret-looking text"

# --- GUI starts and stays up (needs a desktop session)
$gui = Start-Process (Join-Path $Bundle "bin\screentime-gui.exe") -ArgumentList "-m","screentime.gui.app" -PassThru
Start-Sleep 8
if ($gui.HasExited) {
  Write-Host "GUI exited with code $($gui.ExitCode) within 8 s. Evidence follows."
  foreach ($f in "gui.log","gui.crash") {
    $path = "$tmp\L\ScreenTime\logs\$f"
    if (Test-Path $path) { Write-Host "----- $f"; Get-Content $path -Tail 60 } else { Write-Host "----- $f: not written" }
  }
  # Re-run with the console interpreter so Python/GTK messages (which pythonw discards) land in files.
  $env:GSK_RENDERER = "cairo"
  $rr = Run-Cli $Bundle @("-X","faulthandler","-m","screentime.gui.app") 12
  Write-Host "----- console re-run: exit code $($rr.Code)"; Write-Host $rr.Out; Write-Host $rr.Err
}
Assert (-not $gui.HasExited) "GUI starts and keeps running"
Stop-Process -Id $gui.Id -Force -ErrorAction SilentlyContinue

# --- daemon: starts, holds the lock, second copy exits, stops gracefully, data survives
$daemon = Join-Path $Bundle "bin\screentime-daemon.exe"
$d1 = Start-Process $daemon -ArgumentList "-m","screentime.daemon" -PassThru
Start-Sleep 6
Assert (-not $d1.HasExited) "daemon is running"
$d2 = Start-Process $daemon -ArgumentList "-m","screentime.daemon" -PassThru -Wait
Assert ($d2.HasExited) "second daemon exits (single instance)"
Assert (-not $d1.HasExited) "first daemon unaffected by the second"
$r = Run-Cli $Bundle @("-c","from screentime.platform.windows import autostart as a; import sys; sys.exit(0 if a.stop_now() else 1)")
Start-Sleep 2
Assert ($d1.HasExited) "daemon stopped gracefully via the stop event"
Assert (Test-Path "$tmp\L\ScreenTime\screentime.sec") "encrypted store exists in %LOCALAPPDATA%\ScreenTime"
$head = [IO.File]::ReadAllBytes("$tmp\L\ScreenTime\screentime.sec")[0..15]
Assert (-not ([Text.Encoding]::ASCII.GetString($head) -match "SQLite")) "store is not a plaintext SQLite file"

# --- startup task: create, verify, remove (private task name so a real install is untouched)
$r = Run-Cli $Bundle @("-c","from screentime.platform.windows import autostart as a; a.TASK_NAME='\\ScreenTimeSmoke\\Daemon'; a.start_now=lambda: True; a.enable(); i=a.query_task(); print('TASK', i.exists, i.enabled, a.task_is_current(i)); a.disable(); print('GONE', not a.query_task().exists)")
Assert ($r.Out -match "TASK True True True" -and $r.Out -match "GONE True") "startup task created, current, and removed ($($r.Err.Trim()))"

if ($Mode -eq "Installer") {
  $uninst = Get-ChildItem $InstallDir -Filter "unins*.exe" | Select-Object -First 1
  $p = Start-Process $uninst.FullName -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
  Assert ($p.ExitCode -eq 0) "silent uninstall exits 0"
  Start-Sleep 2
  Assert (-not (Test-Path "$InstallDir\bin")) "program files removed"
  Assert (-not (Get-ScheduledTask -TaskName "Daemon" -TaskPath "\ScreenTime\" -ErrorAction SilentlyContinue)) "startup task removed"
  Assert (Test-Path "$tmp\L\ScreenTime\screentime.sec") "usage data kept by a default uninstall"
}
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Write-Host "SMOKE OK ($Mode)"
