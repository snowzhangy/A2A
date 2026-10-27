@echo off
setlocal
cd /d "%~dp0"
if not exist "runtime\.venv\Scripts\pythonw.exe" (
    echo A2A's Windows environment is not installed yet.
    echo Run: uv sync --project runtime --locked --extra gpu
    pause
    exit /b 1
)
"%~dp0runtime\.venv\Scripts\python.exe" -c "import a2a_runtime.ui"
if errorlevel 1 (
    echo A2A could not load its Windows interface. Repair the environment with:
    echo uv sync --project runtime --locked --extra gpu
    pause
    exit /b 1
)
start "" "%~dp0runtime\.venv\Scripts\pythonw.exe" -m a2a_runtime.ui
