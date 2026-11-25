@echo off
setlocal
cd /d "%~dp0"
if defined LEDGER_PYTHON (
    "%LEDGER_PYTHON%" app.py --open
    exit /b
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import numpy, pypdf" >nul 2>&1
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" app.py --open
        exit /b
    )
)
for %%P in ("python" "py -3.12" "py -3") do (
    %%~P -c "import numpy, pypdf" >nul 2>&1
    if not errorlevel 1 (
        %%~P app.py --open
        exit /b
    )
)
echo Install Python 3.12 or later and run the setup commands in START-HERE.txt:
echo py -3.12 -m venv .venv
echo .\.venv\Scripts\python.exe -m pip install --no-cache-dir -r requirements.txt
pause
exit /b 1
