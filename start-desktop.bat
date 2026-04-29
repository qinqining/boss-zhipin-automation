@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

rem 这个脚本设计为“放在桌面也能用”
rem 修改为你的项目实际路径（默认就是当前仓库路径）
set "PROJECT_DIR=D:\boss-zhipin-automation"

if not exist "%PROJECT_DIR%\backend\app\main.py" (
  echo [错误] 未找到项目目录: "%PROJECT_DIR%"
  echo [提示] 请编辑 start-desktop.bat，把 PROJECT_DIR 改成你的实际路径
  pause
  exit /b 1
)

pushd "%PROJECT_DIR%"

echo.
echo ============================================
echo    Boss直聘自动化工具 - 启动中...
echo ============================================
echo.

call "%PROJECT_DIR%\start.bat"

popd
