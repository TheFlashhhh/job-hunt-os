@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ===================================================================
REM  Job Hunt OS - one-click start
REM
REM  Starts the backend (uvicorn, port 8000) and the frontend (Next dev,
REM  port 3000), then opens the browser once the frontend answers.
REM
REM  Only starts what is not already listening, so double-clicking this
REM  twice does not produce two of anything.
REM
REM  Nothing here touches the JobHuntOS-StructuredCollection scheduled
REM  task. This controls the interactive web app and nothing else.
REM ===================================================================

REM %~dp0 is this file's own folder, so a Desktop shortcut with any
REM working directory still resolves the repo correctly.
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

set "PYEXE=%REPO%\.venv\Scripts\python.exe"
set "WEBDIR=%REPO%\web"
set "APPURL=http://localhost:3000"

echo.
echo  Job Hunt OS
echo  %REPO%
echo.

REM ------------------------------------------------------------------
REM  Prerequisites. Fail with something readable rather than opening
REM  two command windows that die immediately.
REM ------------------------------------------------------------------
if not exist "%PYEXE%" (
    echo  [ERROR] Python virtualenv not found:
    echo          %PYEXE%
    echo.
    echo  Create it and install requirements, then run this again.
    echo.
    pause
    exit /b 1
)

if not exist "%WEBDIR%\package.json" (
    echo  [ERROR] Frontend not found:
    echo          %WEBDIR%\package.json
    echo.
    pause
    exit /b 1
)

if not exist "%WEBDIR%\node_modules" (
    echo  [ERROR] Frontend dependencies are not installed:
    echo          %WEBDIR%\node_modules is missing
    echo.
    echo  Run this once, by hand, then use this launcher:
    echo          cd /d "%WEBDIR%" ^&^& npm install
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM  Backend - port 8000
REM ------------------------------------------------------------------
call :IsListening 8000
if "!PORT_BUSY!"=="1" (
    echo  [skip]  Backend already listening on port 8000
) else (
    echo  [start] Backend  ^-^> http://127.0.0.1:8000
    start "Job Hunt OS - Backend" cmd /k "cd /d "%REPO%" && "%PYEXE%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
)

REM ------------------------------------------------------------------
REM  Frontend - port 3000
REM ------------------------------------------------------------------
call :IsListening 3000
if "!PORT_BUSY!"=="1" (
    echo  [skip]  Frontend already listening on port 3000
) else (
    echo  [start] Frontend ^-^> %APPURL%
    start "Job Hunt OS - Frontend" cmd /k "cd /d "%WEBDIR%" && npm run dev"
)

REM ------------------------------------------------------------------
REM  Wait for the frontend to actually answer. Next compiles on the
REM  first request, so this probes rather than sleeping a fixed guess.
REM  Bounded: it gives up and tells the user instead of hanging.
REM ------------------------------------------------------------------
echo.
echo  Waiting for the frontend to come up ...

REM  Readiness = the frontend is accepting connections on port 3000.
REM
REM  It deliberately does NOT fetch the page first. Two earlier attempts
REM  did, and both were worse:
REM
REM    * A `^|` escape inside a quoted argument reaches PowerShell
REM      literally, so the probe never parsed and reported "not ready"
REM      while the site was serving 200s.
REM    * Invoke-WebRequest's -TimeoutSec does not reliably fire when Next
REM      accepts the connection and then holds it during a compile. One
REM      call blocked long enough to run 300s past a 240s deadline, since
REM      the deadline is only re-checked between iterations.
REM
REM  Requesting the page also costs ~20s of PowerShell and .NET warmup
REM  per run, for information the browser is about to obtain anyway.
REM  netstat is instant, has no such failure mode, and the browser waits
REM  out the compile exactly as it does on a normal `npm run dev`.
REM
REM  Bound: 120 iterations of ~2s, so it gives up near 240s rather than
REM  ever waiting forever. `ping` is the sleep because `timeout` refuses
REM  to run when stdin is redirected.
set "READY=0"
for /l %%i in (1,1,120) do (
    if "!READY!"=="0" (
        call :IsListening 3000
        if "!PORT_BUSY!"=="1" (
            set "READY=1"
        ) else (
            ping -n 3 127.0.0.1 >nul 2>&1
        )
    )
)

echo.
if "!READY!"=="1" (
    echo  Opening %APPURL%
    echo.
    echo  The first page can take up to a minute while Next compiles it.
    echo  That is normal - leave the tab open and it will load.
    start "" "%APPURL%"
) else (
    echo  [WARN] Port 3000 never started listening.
    echo         The servers were left running - check the
    echo         "Job Hunt OS - Backend" and "Job Hunt OS - Frontend"
    echo         windows for the error, then open %APPURL% yourself.
    echo.
    pause
)

endlocal
exit /b 0

REM ===================================================================
REM  :IsListening <port>  ->  PORT_BUSY = 1 when something is listening
REM
REM  Matched against the LOCAL address column only, and only on
REM  LISTENING lines, so an unrelated outbound connection to a remote
REM  :3000 is not mistaken for our own server.
REM ===================================================================
:IsListening
set "PORT_BUSY=0"
for /f "tokens=*" %%L in ('netstat -ano -p TCP 2^>nul ^| findstr /R /C:":%~1 .*LISTENING"') do (
    set "PORT_BUSY=1"
)
exit /b 0
