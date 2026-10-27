@echo off
cd /d "%~dp0"
py -3.12 station_app.py %*
if errorlevel 1 pause
