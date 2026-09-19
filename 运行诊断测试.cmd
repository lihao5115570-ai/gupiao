@echo off
setlocal
set "APP_DIR=%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
"%PY%" -m unittest discover -s "%APP_DIR%tests" -v
pause
endlocal
