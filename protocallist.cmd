@echo off
rem Protocallist launcher - double-click to start (or restart) the program.
rem A few checks run in this window first: each prints a line, missing libraries
rem are offered for install, and everything lands in
rem %USERPROFILE%\.protocallist\logs\startup.log. Then the icon appears in the tray
rem and this window closes. Safe to click "just in case": a program that is
rem running already is left alone unless you answer "y" to the restart question.
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo Creating the virtual environment in %ROOT%.venv ...
  py -3.12 -m venv "%ROOT%.venv"
  if errorlevel 1 (
    echo Could not create it: install 64-bit Python 3.12 from python.org and run this again.
    pause
    exit /b 1
  )
)
"%ROOT%.venv\Scripts\python.exe" -m protocallist.tray.startup || pause
