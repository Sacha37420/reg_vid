@echo off
setlocal

if not exist .venv\Scripts\pythonw.exe (
    echo [ERREUR] Le venv n'est pas installe.
    echo          Lancez d'abord install.bat
    pause
    exit /b 1
)

start "" .venv\Scripts\pythonw.exe recorder.py
