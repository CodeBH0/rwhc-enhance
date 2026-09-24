@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-winui.ps1" %*
exit /b %errorlevel%
