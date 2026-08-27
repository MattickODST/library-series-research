@echo off
setlocal

set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

title Library Series Research - START

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment not found.
    echo Run: python -m venv .venv
    exit /b 1
)

pushd "%ROOT%"

echo === Library Series Research ===
echo Starting local Gemma research worker...
echo Press Ctrl+C in this window to stop gracefully.
echo.

"%PYTHON%" "src\runner.py" run

set "RC=%ERRORLEVEL%"
popd

exit /b %RC%
