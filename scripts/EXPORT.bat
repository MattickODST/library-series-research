@echo off
setlocal

set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

title Library Series Research - EXPORT

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment not found.
    exit /b 1
)

pushd "%ROOT%"

"%PYTHON%" "src\export.py"

set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
    echo.
    echo Export complete.
) else (
    echo.
    echo ERROR: Export failed.
)

popd
pause
exit /b %RC%
