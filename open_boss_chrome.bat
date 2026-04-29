@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Open Boss in REAL Chrome (no Playwright)
echo ============================================
echo.

set "URL=https://www.zhipin.com/web/user/?ka=header-login"
set "PROFILE_DIR=%~dp0.boss_real_chrome_profile"

set "CHROME_EXE="

REM Try common Chrome install locations
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"

REM Try edge as fallback (some machines don't have Chrome)
set "EDGE_EXE="
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE_EXE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE_EXE if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE_EXE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not exist "%PROFILE_DIR%" (
  mkdir "%PROFILE_DIR%" >nul 2>nul
)

if defined CHROME_EXE (
  echo [OK] Using Chrome: "%CHROME_EXE%"
  echo [OK] Profile dir: "%PROFILE_DIR%"
  echo [OK] Opening: %URL%
  echo.
  start "" "%CHROME_EXE%" ^
    --user-data-dir="%PROFILE_DIR%" ^
    --no-first-run ^
    --no-default-browser-check ^
    "%URL%"
  goto :eof
)

if defined EDGE_EXE (
  echo [WARN] Chrome not found. Using Edge: "%EDGE_EXE%"
  echo [OK] Profile dir: "%PROFILE_DIR%"
  echo [OK] Opening: %URL%
  echo.
  start "" "%EDGE_EXE%" ^
    --user-data-dir="%PROFILE_DIR%" ^
    --no-first-run ^
    --no-default-browser-check ^
    "%URL%"
  goto :eof
)

echo [ERROR] Neither Chrome nor Edge was found.
echo - Please install Google Chrome, or check your installation path.
echo - Then re-run this script.
echo.
pause
exit /b 1

