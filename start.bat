@echo off
setlocal
title CodeScout
echo [CHECK] Checking Python 3.11 or newer...
pushd "%~dp0"
if errorlevel 1 goto directory_error

call python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "CODESCOUT_PYTHON=python"
    goto check_dependencies
)
call py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "CODESCOUT_PYTHON=py -3"
    goto check_dependencies
)
echo [ERROR] Python 3.11 or newer was not found or could not run.
echo Install Python from python.org and enable Add Python to PATH, then try again.
set "CODESCOUT_EXIT=1"
goto failed

:check_dependencies
echo [CHECK] Checking CodeScout and Python dependencies...
call %CODESCOUT_PYTHON% -c "import sqlite3, tkinter; import codescout.__main__"
if errorlevel 1 (
    echo [ERROR] Required Python dependencies or CodeScout files are unavailable.
    echo Keep this launcher beside the codescout folder and install Python with Tcl/Tk and SQLite.
    set "CODESCOUT_EXIT=1"
    goto failed
)
echo [START] Starting CodeScout. The browser opens after the service is ready.
echo Keep this window open while using CodeScout. Press Ctrl+C to stop.
call %CODESCOUT_PYTHON% -m codescout --open
set "CODESCOUT_EXIT=%ERRORLEVEL%"
if not "%CODESCOUT_EXIT%" == "0" (
    echo [ERROR] CodeScout stopped with exit code %CODESCOUT_EXIT%.
    echo Read the error above. Check the port, project files, and data directory access.
    goto failed
)
echo [STOP] CodeScout stopped.
popd
exit /b 0

:directory_error
echo [ERROR] Cannot access the launcher folder.
set "CODESCOUT_EXIT=1"
goto wait_and_exit

:failed
popd
:wait_and_exit
echo [WAIT] Press any key to close this window.
pause >nul
exit /b %CODESCOUT_EXIT%
