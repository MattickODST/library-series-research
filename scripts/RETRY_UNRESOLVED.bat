@echo off
setlocal

set "ROOT=%~dp0.."
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

title Library Series Research - RETRY UNRESOLVED

if not exist "%PYTHON%" (
    echo ERROR: Virtual environment not found.
    exit /b 1
)

pushd "%ROOT%"

echo Requeueing:
echo   CONFLICT
echo   UNFOUND
echo   TIMED_OUT
echo   ERROR
echo.

"%PYTHON%" "src\batch.py" retry

set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Retry queue update failed.
)

popd
pause
exit /b %RC%
