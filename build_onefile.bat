@echo off
set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [build] Missing project Python: "%PYTHON%"
    exit /b 1
)

rem PyInstaller can finish successfully after excluding a broken Tkinter.
rem Check Tcl startup before removing any previous build artifacts.
"%PYTHON%" -c "import tkinter; tkinter.Tcl()"
if errorlevel 1 (
    echo [build] Tkinter/Tcl is unavailable; refusing to produce an incomplete EXE.
    exit /b 1
)

cd /d "%~dp0tdp-modules"

echo [build] Working directory: %CD%
"%PYTHON%" --version
"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install pyinstaller
)

rem Never erase an entire dist folder: older EXEs may have user music and profiles beside them.
echo [build] Running PyInstaller...
"%PYTHON%" -m PyInstaller --noconfirm --distpath dist-current --workpath build-current TvCGUI_onefile.spec

if errorlevel 1 (
    echo [build] FAILED.
    pause
    exit /b 1
)

rem A successful PyInstaller exit can still leave Tkinter out of the EXE.
rem Music and settings windows require both the extension and Tcl/Tk hook.
findstr /C:"pyi_rth__tkinter" build-current\TvCGUI_onefile\Analysis-00.toc >nul
if errorlevel 1 (
    echo [build] FAILED: Tkinter runtime hook is missing from the EXE.
    exit /b 1
)
findstr /C:"_tkinter.pyd" build-current\TvCGUI_onefile\Analysis-00.toc >nul
if errorlevel 1 (
    echo [build] FAILED: _tkinter.pyd is missing from the EXE.
    exit /b 1
)

echo [build] SUCCESS: %CD%\dist-current\TvCGUI.exe
pause
