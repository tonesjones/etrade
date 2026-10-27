@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project virtual environment not found.
    exit /b 1
)

set "engine=%~1"
if "%engine%"=="" set "engine=chatgpt"
".venv\Scripts\python.exe" "import_response.py" --engine "%engine%" --from-clipboard
exit /b %errorlevel%
