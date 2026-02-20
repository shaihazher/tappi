@echo off
:: tappi launcher for Windows — double-click to start
:: Activates venv, launches browser, starts web UI, opens in browser

set VENV_DIR=%USERPROFILE%\.tappi-venv

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo.
    echo   tappi not installed. Run the installer first:
    echo   irm https://raw.githubusercontent.com/shaihazher/tappi/main/install/install-windows.ps1 ^| iex
    echo.
    pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"

echo.
echo   Starting tappi...
echo.

:: Launch browser in background
start /b bpy launch >nul 2>&1
timeout /t 2 /nobreak >nul

:: Start web UI (foreground — keeps window open)
start /b bpy serve
timeout /t 2 /nobreak >nul

:: Open in default browser
start http://127.0.0.1:8321

echo.
echo   tappi is running at http://127.0.0.1:8321
echo   Close this window to stop the server.
echo.
pause
