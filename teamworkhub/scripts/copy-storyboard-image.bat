@echo off
set SRC=%USERPROFILE%\.cursor\projects\c-Users-ParkEunJin-teamworkhub\assets\storyboard-flow-ko.png
set DEST=%~dp0..\docs\storyboard-flow-ko.png
if not exist "%SRC%" (
  echo Source not found
  exit /b 1
)
mkdir "%~dp0..\docs" 2>nul
copy /Y "%SRC%" "%DEST%"
