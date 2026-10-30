@echo off
setlocal
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onedir --windowed --name EVA app.py
if errorlevel 1 (
    echo ERRO ao compilar com PyInstaller.
    pause
    exit /b 1
)
copy /Y "config.json" "dist\EVA\config.json" >nul
echo.
echo EXE: dist\EVA\EVA.exe
echo O FFmpeg e o FFprobe precisam estar no PATH do Windows.
echo Este EXE nao esta homologado para playout.
pause
