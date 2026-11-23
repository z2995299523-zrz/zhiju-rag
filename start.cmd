@echo off
setlocal
pushd "%~dp0" || exit /b 1
where py.exe >nul 2>nul
if errorlevel 1 (
    python.exe "%~dp0launch.py" %*
) else (
    py.exe -3 "%~dp0launch.py" %*
)
set "launch_status=%errorlevel%"
popd
if not "%launch_status%"=="0" pause
exit /b %launch_status%
