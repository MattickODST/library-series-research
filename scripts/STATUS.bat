@echo off
setlocal

set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

title Library Series Research - STATUS

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment not found.
    exit /b 1
)

pushd "%ROOT%"
"%PYTHON%" "src\batch.py" status
set "RC=%ERRORLEVEL%"
popd

pause
exit /b %RC%
