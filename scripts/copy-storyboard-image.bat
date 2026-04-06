@echo off
REM Copies AI-generated storyboard PNG into docs/ for STORYBOARD.md preview.
set "SRC=%USERPROFILE%\.cursor\projects\c-Users-ParkEunJin-teamworkhub\assets\storyboard-flow-ko.png"
set "DEST=%~dp0..\docs\storyboard-flow-ko.png"
if not exist "%SRC%" (
  echo Source not found: %SRC%
  exit /b 1
)
mkdir "%~dp0..\docs" 2>nul
copy /Y "%SRC%" "%DEST%"
echo OK: %DEST%
