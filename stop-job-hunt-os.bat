@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ===================================================================
REM  Job Hunt OS - one-click stop
REM
REM  Stops ONLY the processes listening on port 8000 (backend) and
REM  port 3000 (frontend).
REM
REM  Deliberately NOT "taskkill /IM python.exe" or "/IM node.exe" -
REM  either would kill unrelated Python or Node work that has nothing
REM  to do with this app.
REM
REM  Nothing here touches the JobHuntOS-StructuredCollection scheduled
REM  task.
REM ===================================================================

echo.
echo  Stopping Job Hunt OS
echo.

set "STOPPED=0"

call :StopPort 8000 Backend
call :StopPort 3000 Frontend

echo.
if "!STOPPED!"=="0" (
    echo  Nothing was running.
) else (
    echo  Done.
)
echo.

endlocal
exit /b 0

REM ===================================================================
REM  :StopPort <port> <label>
REM
REM  Finds the PID of whatever is LISTENING on the port and terminates
REM  that process tree. /T covers the child processes a dev server
REM  spawns; without it the port can stay held by a survivor.
REM ===================================================================
:StopPort
set "PORT=%~1"
set "LABEL=%~2"
set "FOUND=0"

for /f "tokens=5" %%P in ('netstat -ano -p TCP 2^>nul ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not "%%P"=="0" (
        if "!FOUND!"=="0" (
            set "FOUND=1"
            set "STOPPED=1"
            echo  [stop]  %LABEL% ^(port %PORT%, PID %%P^)
            taskkill /PID %%P /T /F >nul 2>&1
            if errorlevel 1 (
                echo  [WARN]  Could not stop PID %%P - it may have already exited.
            )
        )
    )
)

if "!FOUND!"=="0" echo  [skip]  %LABEL% ^(port %PORT%^) was not running
exit /b 0
