@echo off
rem Opens the Iris setup window. Uses the Python launcher if present, else python on PATH.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m iris.setup_gui
) else (
    python -m iris.setup_gui
)
if errorlevel 1 (
    echo.
    echo Could not start the setup window. Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install, then run setup.bat again.
    pause
)
