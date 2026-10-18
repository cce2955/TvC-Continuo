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
if errorlevel 1 (
    echo [build] Could not enter the tdp-modules directory.
    pause
    exit /b 1
)

echo [build] Working directory: %CD%
"%PYTHON%" --version
"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    "%PYTHON%" -m pip install pyinstaller
)

rem Remove only PyInstaller's generated analysis for this app. A clean work
rem directory forces Analysis, PYZ, PKG, and EXE to be rebuilt on every run.
rem Never erase dist-current: music, profiles, and other user data may live there.
echo [build] Clearing previous PyInstaller analysis...
powershell -NoProfile -NonInteractive -Command "$workspace=[IO.Path]::GetFullPath((Get-Location).Path); $build=[IO.Path]::GetFullPath((Join-Path $workspace 'build-current')); $target=[IO.Path]::GetFullPath((Join-Path $build 'TvCGUI_onefile')); if (-not $build.StartsWith($workspace+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or -not $target.StartsWith($build+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Build path escaped the workspace' }; if (Test-Path -LiteralPath $build) { $parent=Get-Item -LiteralPath $build -Force; if ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Build directory is a link' } }; if (Test-Path -LiteralPath $target) { $item=Get-Item -LiteralPath $target -Force; if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Analysis directory is a link' }; Remove-Item -LiteralPath $target -Recurse -Force }"
if errorlevel 1 (
    echo [build] FAILED: Could not clear the generated analysis safely.
    pause
    exit /b 1
)

echo [build] Building fresh one-file EXE with PyInstaller...
"%PYTHON%" -m PyInstaller --clean --noconfirm --distpath dist-current --workpath build-current TvCGUI_onefile.spec

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

if not exist "dist-current\TvCGUI.exe" (
    echo [build] FAILED: PyInstaller did not produce TvCGUI.exe.
    pause
    exit /b 1
)

echo [build] SUCCESS: %CD%\dist-current\TvCGUI.exe
powershell -NoProfile -NonInteractive -Command "$file=Get-Item -LiteralPath 'dist-current\TvCGUI.exe'; $sha=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash; Write-Host ('[build] Size: {0:N1} MB  SHA-256: {1}' -f ($file.Length / 1MB), $sha)"
pause
