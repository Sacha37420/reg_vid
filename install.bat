@echo off
setlocal

echo ============================================
echo  Installation de l'enregistreur d'ecran
echo ============================================
echo.

rem --- Trouver Python (on ignore le stub Windows Store) ---
set PYTHON=

for %%P in (python python3) do (
    if not defined PYTHON (
        %%P -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
        if not errorlevel 1 set PYTHON=%%P
    )
)

if not defined PYTHON (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "%USERPROFILE%\.conda\envs\python311\python.exe"
        "%USERPROFILE%\.conda\envs\base\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
    ) do (
        if not defined PYTHON (
            if exist %%P set PYTHON=%%P
        )
    )
)

if not defined PYTHON (
    echo [ERREUR] Python 3.9+ introuvable.
    echo          Installez Python depuis https://python.org
    pause
    exit /b 1
)

echo Python trouve : %PYTHON%
%PYTHON% --version
echo.

rem --- Creer le venv si necessaire ---
if exist .venv\Scripts\python.exe (
    echo Venv deja present - mise a jour des dependances uniquement.
    goto install_deps
)

echo Creation du venv .venv ...
%PYTHON% -m venv .venv
if errorlevel 1 (
    echo [ERREUR] Impossible de creer le venv.
    pause
    exit /b 1
)

:install_deps
echo.
echo Mise a jour de pip...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet

echo.
echo Installation des dependances (dont ffmpeg via imageio-ffmpeg)...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] L'installation des dependances a echoue.
    pause
    exit /b 1
)

echo.
echo Verification de ffmpeg...
.venv\Scripts\python.exe -c "import imageio_ffmpeg; print('ffmpeg : OK ->', imageio_ffmpeg.get_ffmpeg_exe())"

echo.
echo ============================================
echo  Installation terminee !
echo  Lancez l'app avec : run.bat
echo ============================================
echo.
pause