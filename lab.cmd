@echo off
REM Thin wrapper: everything lives in lab.py (Python 3.10+, standard library only).
REM Usage: lab setup ^| lab status ^| lab dc-down a ^| lab partition a ^| lab heal a ^| lab help
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0lab.py" %*
) else (
    python "%~dp0lab.py" %*
)
exit /b %errorlevel%
