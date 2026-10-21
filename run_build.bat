@echo off
setlocal enabledelayedexpansion
title Auto Builder - KMZ DXF Converter

echo ========================================================
echo   KMZ ^<^-^> DXF Converter - Automatic Build Script
echo ========================================================
echo.

REM 1. Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python from https://python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

REM 2. Create requirements.txt and workflow file
echo [1/6] Preparing configuration files...
echo ezdxf>=1.1> requirements.txt
echo pyproj>> requirements.txt

if not exist ".github\workflows" mkdir ".github\workflows"

(
echo name: Build EXE
echo on: [push, workflow_dispatch]
echo jobs:
echo   build:
echo     runs-on: windows-latest
echo     steps:
echo       - uses: actions/checkout@v4
echo       - uses: actions/setup-python@v5
echo         with:
echo           python-version: '3.10'
echo           cache: 'pip'
echo       - name: Install dependencies
echo         run: ^|
echo           python -m pip install --upgrade pip
echo           pip install -r requirements.txt
echo           pip install pyinstaller
echo       - name: Build EXE
echo         run: pyinstaller --onefile --windowed --name kmz_dxf_gui --hidden-import kmz_dxf_converter_v2 kmz_dxf_gui.py
echo       - name: Upload artifact
echo         uses: actions/upload-artifact@v4
echo         with:
echo           name: kmz_dxf_exe
echo           path: dist/kmz_dxf_gui.exe
echo           if-no-files-found: error
) > ".github\workflows\build.yml"

echo       Done.
echo.

REM 3. Init Git and commit
echo [2/6] Initializing Git repository...
if not exist ".git" (
    git init
    git config user.name "Kotsh"
    git config user.email "kotsh@users.noreply.github.com"
    git branch -M main
)
git add -A
git commit -m "Auto-build: KMZ DXF Converter with GUI" >nul 2>&1
echo       Done.
echo.

REM 4. Create GitHub repo and push
echo [3/6] Creating GitHub repository and pushing...
set REPO_NAME=kmz-dxf-converter
gh repo view !REPO_NAME! >nul 2>&1
if errorlevel 1 (
    echo       Creating repo !REPO_NAME!...
    gh repo create !REPO_NAME! --public --description "KMZ DXF Converter" --confirm
) else (
    echo       Repo !REPO_NAME! already exists, reusing.
)
for /f "tokens=*" %%i in ('gh api user --jq .login') do set GH_USER=%%i
git remote remove origin >nul 2>&1
git remote add origin https://github.com/!GH_USER!/!REPO_NAME!.git
git push -u origin main --force
echo       Done.
echo.

REM 5. Trigger build
echo [4/6] Triggering GitHub Actions build...
gh workflow run build.yml
timeout /t 8 /nobreak >nul
for /f "tokens=*" %%i in ('gh run list --limit 1 --json databaseId --jq ".[0].databaseId"') do set RUN_ID=%%i
echo       Build ID: !RUN_ID!
echo.

REM 6. Wait for build
echo [5/6] Waiting for build to finish (3-8 minutes)...
gh run watch !RUN_ID!
echo.

REM 7. Download artifact
echo [6/6] Downloading EXE...
if exist "kmz_dxf_exe" rmdir /s /q "kmz_dxf_exe"
gh run download !RUN_ID! -n kmz_dxf_exe
echo       Downloaded.
echo.

REM Move the exe to the current folder
for /r "kmz_dxf_exe" %%f in (*.exe) do (
    move "%%f" "kmz_dxf_gui.exe" >nul
    echo       EXE saved as: kmz_dxf_gui.exe
    goto :done
)
:done

echo.
echo ========================================================
echo   DONE! Your EXE is ready: kmz_dxf_gui.exe
echo ========================================================
pause