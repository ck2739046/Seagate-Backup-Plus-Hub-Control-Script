@echo off
where python >nul 2>nul && (
    python "%~dp0BackupPlusHub-Tool.py" %*
    if errorlevel 1 pause
    exit /b
)

echo.
echo Python not found. Please install Python 3.8+ and try again.
echo.
pause
