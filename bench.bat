@echo off
REM Iris accuracy bench launcher. Run: bench.bat [args]
REM e.g. bench.bat --scenarios baseline_static
pushd "%~dp0"
python -m iris.bench.runner %*
set RC=%ERRORLEVEL%
popd
exit /b %RC%
