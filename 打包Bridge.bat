@echo off
REM ===================================================================
REM  打包 BMPHWID_Bridge.py -> dist\BMPHWID_Bridge.exe  (单文件、无黑框)
REM  首次执行会 pip install 缺失的依赖 (pyinstaller / pywin32)
REM ===================================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo [1/3] 检查/安装依赖 ...
where python >nul 2>nul || (echo ❌ 没安装 Python 3.10+ & pause & exit /b 1)

python -c "import PyInstaller" 2>nul
if errorlevel 1 (pip install pyinstaller==6.10.0 || goto :err)

python -c "import win32gui" 2>nul
if errorlevel 1 (pip install pywin32==306 || goto :err)

echo.
echo [2/3] 清理旧构建 ...
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist BMPHWID_Bridge.spec del /q BMPHWID_Bridge.spec

echo.
echo [3/3] PyInstaller 打包 ...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name BMPHWID_Bridge ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.scrolledtext ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import win32api ^
    --hidden-import win32clipboard ^
    BMPHWID_Bridge.py || goto :err

echo.
echo ============================================================
echo  ✅ 打包完成
echo     输出: dist\BMPHWID_Bridge.exe
echo     双击直接运行 (无需 Python，无需安装)
echo ============================================================
pause
exit /b 0

:err
echo.
echo ❌ 打包失败，看上面错误信息
pause
exit /b 1
