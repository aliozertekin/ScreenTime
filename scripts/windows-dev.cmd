@echo off
rem ScreenTime on Windows, run from this source checkout (no installer needed).
rem Uses MSYS2's MINGW64 Python + GTK4, the same runtime the installer bundles.
rem
rem   scripts\windows-dev.cmd setup      one-time: install GTK4/libadwaita/Python deps and ScreenTime
rem   scripts\windows-dev.cmd gui        open the dashboard
rem   scripts\windows-dev.cmd daemon     run the tracker in the background (no window)
rem   scripts\windows-dev.cmd diagnose   print the diagnostics report
rem   scripts\windows-dev.cmd test       run the test suite
rem
rem Set SCREENTIME_MSYS2 if MSYS2 is not in C:\msys64. Installer: https://www.msys2.org
setlocal
if "%SCREENTIME_MSYS2%"=="" set "SCREENTIME_MSYS2=C:\msys64"
set "BIN=%SCREENTIME_MSYS2%\mingw64\bin"
set "REPO=%~dp0.."
set "CMD=%~1"
if "%CMD%"=="" goto usage

if /i "%CMD%"=="setup" goto setup
if not exist "%BIN%\python.exe" (
  echo MSYS2 / its Python was not found in "%SCREENTIME_MSYS2%".
  echo Install MSYS2 from https://www.msys2.org, then run:  scripts\windows-dev.cmd setup
  exit /b 1
)
set "PATH=%BIN%;%PATH%"
set "PYTHONPATH=%REPO%"

if /i "%CMD%"=="gui"      ( start "" "%BIN%\pythonw.exe" -m screentime.gui.app & exit /b 0 )
if /i "%CMD%"=="daemon"   ( start "" "%BIN%\pythonw.exe" -m screentime.daemon & exit /b 0 )
if /i "%CMD%"=="diagnose" ( "%BIN%\python.exe" -m screentime.diagnostics & pause & exit /b 0 )
if /i "%CMD%"=="test"     ( "%BIN%\python.exe" -m pytest -q "%REPO%\tests" & exit /b %ERRORLEVEL% )
goto usage

:setup
if not exist "%SCREENTIME_MSYS2%\usr\bin\bash.exe" (
  echo MSYS2 was not found in "%SCREENTIME_MSYS2%". Install it from https://www.msys2.org first.
  exit /b 1
)
set "MSYSTEM=MINGW64"
set "CHERE_INVOKING=1"
"%SCREENTIME_MSYS2%\usr\bin\bash.exe" -lc "pacman -S --needed --noconfirm mingw-w64-x86_64-python mingw-w64-x86_64-python-pip mingw-w64-x86_64-python-setuptools mingw-w64-x86_64-python-wheel mingw-w64-x86_64-python-gobject mingw-w64-x86_64-python-cairo mingw-w64-x86_64-python-psutil mingw-w64-x86_64-python-cryptography mingw-w64-x86_64-python-pytest mingw-w64-x86_64-gtk4 mingw-w64-x86_64-libadwaita mingw-w64-x86_64-librsvg mingw-w64-x86_64-adwaita-icon-theme"
if errorlevel 1 exit /b 1
rem Install ScreenTime itself so the start-at-login task (which runs plain "pythonw -m screentime.daemon") can import it.
"%BIN%\python.exe" -m pip install --no-deps --force-reinstall --break-system-packages "%REPO%"
if errorlevel 1 exit /b 1
echo.
echo Done. Open the app with:  scripts\windows-dev.cmd gui
echo After updating the source ^(git pull^), run  scripts\windows-dev.cmd setup  again so the startup task uses the new code.
exit /b 0

:usage
echo Usage: scripts\windows-dev.cmd setup ^| gui ^| daemon ^| diagnose ^| test
exit /b 2
