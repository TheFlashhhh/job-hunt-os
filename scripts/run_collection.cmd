@echo off
REM ---------------------------------------------------------------------------
REM Structured ATS collection, for Windows Task Scheduler.
REM
REM Task Scheduler is the honest local answer to "when does this run?". An
REM in-process scheduler inside uvicorn could only run while the machine is
REM awake AND the server happens to be up, which would make "collection is
REM scheduled" a claim the product could not keep.
REM
REM This wrapper exists so the registered task is one quoted path with no
REM nested quotes, and so the repository location is derived rather than baked
REM in: %~dp0 is this script's own directory.
REM
REM Every command below is idempotent, resumable and catch-up safe. A repeat
REM poll of an unchanged board writes nothing, so a missed window costs nothing
REM and there is no catch-up storm to guard against.
REM
REM Register it with:  python -m app.cli schedule --register
REM Show it with:      python -m app.cli schedule
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m app.cli collect %*
) else (
    python -m app.cli collect %*
)

set EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
