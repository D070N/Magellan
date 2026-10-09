@echo off
setlocal EnableExtensions
title Magellan - Windows EXE Builder

echo ==========================================
echo       Magellan Windows Builder
echo ==========================================
echo.

cd /d "%~dp0"

echo [1/5] Checking Python...
set "PYTHON_CMD="

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=py"
    goto :python_found
)

where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=python"
    goto :python_found
)

echo.
echo ERROR: Python was not found.
echo.
echo Please install Python 3.10 or newer from:
echo https://www.python.org/downloads/windows/
echo.
echo IMPORTANT: During installation, enable "Add python.exe to PATH".
echo.
goto :failed

:python_found
echo Python command: %PYTHON_CMD%
echo.

echo [2/5] Checking pip...
"%PYTHON_CMD%" -m pip --version
if not errorlevel 1 goto :pip_found

echo The Python launcher did not start correctly. Checking the per-user Python install...
set "PYTHON_CMD="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "Get-ChildItem -Path (Join-Path $env:USERPROFILE 'AppData\Local\Python\pythoncore-*\python.exe') -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName"`) do set "PYTHON_CMD=%%P"
if not defined PYTHON_CMD (
    echo.
    echo ERROR: pip is not available in a working Python installation.
    goto :failed
)
"%PYTHON_CMD%" -m pip --version
if errorlevel 1 (
    echo.
    echo ERROR: pip is not available in this Python installation.
    goto :failed
)

:pip_found

echo.
echo [3/5] Installing Magellan dependencies...
"%PYTHON_CMD%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install the required Python packages.
    goto :failed
)

echo.
echo [4/5] Installing PyInstaller...
"%PYTHON_CMD%" -m pip install pyinstaller
if errorlevel 1 (
    echo.
    echo ERROR: Could not install PyInstaller.
    goto :failed
)

echo.
echo [5/5] Building Magellan.exe...
if exist build rmdir /s /q build
if not exist dist mkdir dist

"%PYTHON_CMD%" -m PyInstaller --noconfirm --clean Magellan.spec > build_log.txt 2>&1

if errorlevel 1 (
    echo.
    echo ==========================================
    echo BUILD FAILED
    echo ==========================================
    echo.
    echo The full PyInstaller log was saved to:
    echo %CD%\build_log.txt
    echo.
    echo The last part of the log is:
    powershell -NoProfile -Command "Get-Content build_log.txt -Tail 30"
    goto :failed
)

if not exist "dist\Magellan.exe" (
    echo.
    echo ERROR: PyInstaller reported success but the EXE was not found.
    echo See build_log.txt for details.
    goto :failed
)

echo.
echo ==========================================
echo BUILD SUCCESSFUL
echo ==========================================
echo.
echo Your EXE is here:
echo %CD%\dist\Magellan.exe
echo.
echo You can double-click that EXE to run Magellan.
echo.
goto :done

:failed
echo.
echo ==========================================
echo The build did not complete.
echo ==========================================
echo.
echo This window will stay open so you can read the error.
echo.
:done
pause
