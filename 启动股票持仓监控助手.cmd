@echo off
setlocal
set "APP_DIR=%~dp0"
set "PYW=C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
if not exist "%PYW%" set "PYW=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
start "" "%PYW%" "%APP_DIR%main.py" %*
endlocal
