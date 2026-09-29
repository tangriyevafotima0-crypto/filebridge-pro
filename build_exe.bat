@echo off
echo ============================================
echo   FileBridge Pro - EXE yaratish
echo ============================================

:: PyInstaller o'rnatish
pip install pyinstaller --quiet

:: EXE yaratish
pyinstaller --onefile ^
  --name "FileBridgePro" ^
  --icon NONE ^
  --console ^
  --hidden-import=ctypes ^
  --hidden-import=ctypes.wintypes ^
  --hidden-import=sqlite3 ^
  --hidden-import=mimetypes ^
  --hidden-import=cgi ^
  filebridge_pro.py

echo.
echo ============================================
echo   Tayyor! dist\FileBridgePro.exe
echo ============================================
pause
