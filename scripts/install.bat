@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM StAgent Windows installer (self-contained CMD).
REM Usage:
REM   curl -fsSL https://vixues.com.cn/install.bat -o install.bat && install.bat
REM   scripts\install.bat
REM Optional environment (same as install.sh):
REM   StAgent_GIT_URL StAgent_CLONE_DIR StAgent_REF StAgent_SKIP_START
REM   StAgent_DRY_RUN StAgent_RUN_CHECK StAgent_SKIP_INIT StAgent_INSTALL_RETRIES
REM   UV_SYNC_EXTRAS UV_INDEX_URL UV_VENV_CLEAR
REM Clones or uses a local tree, checks git / Node / uv, then runs ./start.sh via Git Bash.

if not defined StAgent_GIT_URL set "StAgent_GIT_URL=https://github.com/WeiY11/StAgent.git"
if not defined StAgent_CLONE_DIR set "StAgent_CLONE_DIR=%USERPROFILE%\StAgent-desktop"
if not defined StAgent_REF set "StAgent_REF=main"
if not defined UV_SYNC_EXTRAS set "UV_SYNC_EXTRAS=dev browser"
if not defined UV_VENV_CLEAR set "UV_VENV_CLEAR=1"
if not defined StAgent_SKIP_START set "StAgent_SKIP_START=0"
if not defined StAgent_DRY_RUN set "StAgent_DRY_RUN=0"
if not defined StAgent_RUN_CHECK set "StAgent_RUN_CHECK=1"
if not defined StAgent_SKIP_INIT set "StAgent_SKIP_INIT=0"
if not defined StAgent_INSTALL_RETRIES set "StAgent_INSTALL_RETRIES=3"

set "FLAG_SKIPSTART=0"
set "FLAG_DRYRUN=0"
set "FLAG_NOCHECK=0"
set "FLAG_FROMSOURCE=0"
set "FLAG_SKIPINIT=0"

:argloop
if "%~1"=="" goto argdone
if /i "%~1"=="-Help" goto :help
if /i "%~1"=="/?" goto :help
if /i "%~1"=="--help" goto :help
if /i "%~1"=="-SkipStart" set "FLAG_SKIPSTART=1" & shift & goto :argloop
if /i "%~1"=="-SkipInit" set "FLAG_SKIPINIT=1" & shift & goto :argloop
if /i "%~1"=="-DryRun" set "FLAG_DRYRUN=1" & shift & goto :argloop
if /i "%~1"=="-NoCheck" set "FLAG_NOCHECK=1" & shift & goto :argloop
if /i "%~1"=="-FromSource" (
  set "FLAG_FROMSOURCE=1"
  if "%~2"=="" (
    echo [StAgent] -FromSource requires a path 1>&2
    exit /b 1
  )
  set "StAgent_CLONE_DIR=%~2"
  shift & shift
  goto :argloop
)
if /i "%~1"=="-GitUrl" (
  if "%~2"=="" ( echo [StAgent] -GitUrl requires a value 1>&2 & exit /b 1 )
  set "StAgent_GIT_URL=%~2"
  shift & shift
  goto :argloop
)
if /i "%~1"=="-Dir" (
  if "%~2"=="" ( echo [StAgent] -Dir requires a path 1>&2 & exit /b 1 )
  set "StAgent_CLONE_DIR=%~2"
  shift & shift
  goto :argloop
)
if /i "%~1"=="-Ref" (
  if "%~2"=="" ( echo [StAgent] -Ref requires a value 1>&2 & exit /b 1 )
  set "StAgent_REF=%~2"
  shift & shift
  goto :argloop
)
if /i "%~1"=="-Version" (
  if "%~2"=="" ( echo [StAgent] -Version requires a value 1>&2 & exit /b 1 )
  set "StAgent_REF=%~2"
  shift & shift
  goto :argloop
)
if /i "%~1"=="-Extras" (
  if "%~2"=="" ( echo [StAgent] -Extras requires a value 1>&2 & exit /b 1 )
  set "UV_SYNC_EXTRAS=%~2"
  shift & shift
  goto :argloop
)
echo [StAgent] Unknown option: %~1  (try install.bat /?) 1>&2
exit /b 1

:argdone
if "%StAgent_SKIP_START%"=="1" set "FLAG_SKIPSTART=1"
if "%StAgent_DRY_RUN%"=="1" set "FLAG_DRYRUN=1"
if "%StAgent_RUN_CHECK%"=="0" set "FLAG_NOCHECK=1"
if "%StAgent_SKIP_INIT%"=="1" set "FLAG_SKIPINIT=1"

if "%FLAG_FROMSOURCE%"=="1" (
  for %%I in ("%StAgent_CLONE_DIR%") do set "StAgent_CLONE_DIR=%%~fI"
  if not exist "!StAgent_CLONE_DIR!\start.sh" (
    echo [StAgent] start.sh not found in: !StAgent_CLONE_DIR! 1>&2
    exit /b 1
  )
) else (
  for %%I in ("%StAgent_CLONE_DIR%") do set "StAgent_CLONE_DIR=%%~fI"
)

where git >nul 2>nul || (
  echo [StAgent] Git is required: https://git-scm.com/download/win 1>&2
  exit /b 1
)

where node >nul 2>nul || (
  echo [StAgent] Node.js 20.19+ or 22.12+ is required: https://nodejs.org/ 1>&2
  exit /b 1
)
where npm >nul 2>nul || (
  echo [StAgent] npm is required ^(install Node.js LTS^) 1>&2
  exit /b 1
)
for /f "delims=" %%V in ('node -p "const [M,m]=process.versions.node.split('.').map(Number); Number((M === 20 && m >= 19) || (M === 22 && m >= 12) || M > 22)" 2^>nul') do set "NODE_OK=%%V"
if not "!NODE_OK!"=="1" (
  for /f "delims=" %%N in ('node -v 2^>nul') do set "NODE_VER=%%N"
  echo [StAgent] Node.js 20.19+ or 22.12+ required for Vite 7 ^(found !NODE_VER!^) 1>&2
  exit /b 1
)
echo [StAgent] Node OK

set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
  if "!FLAG_DRYRUN!"=="1" (
    echo [StAgent] [dry-run] would install uv via Astral install script
  ) else (
    echo [StAgent] Installing uv...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Invoke-Expression (Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1')"
    if errorlevel 1 (
      echo [StAgent] uv install failed 1>&2
      exit /b 1
    )
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
  )
)
where uv >nul 2>nul
if errorlevel 1 (
  echo [StAgent] uv not on PATH. Add %USERPROFILE%\.local\bin to PATH and retry. 1>&2
  exit /b 1
)
echo [StAgent] uv OK

if not defined UV_INDEX_URL (
  set "UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/"
  curl -fsS --connect-timeout 3 "https://pypi.org/pypi/pip/json" -o nul 2>nul
  if not errorlevel 1 (
    set "UV_INDEX_URL=https://pypi.org/simple/"
    echo [StAgent] Using official PyPI index ^(connectivity OK^)
  ) else (
    echo [StAgent] Using Aliyun PyPI mirror ^(official index unreachable^)
  )
) else (
  echo [StAgent] UV_INDEX_URL already set: !UV_INDEX_URL!
)

echo.
echo [StAgent] Target directory: !StAgent_CLONE_DIR!
echo [StAgent] repository: !StAgent_GIT_URL!
echo [StAgent] ref:        !StAgent_REF!
echo [StAgent] PyPI index: !UV_INDEX_URL!
echo.

if "!FLAG_FROMSOURCE!"=="1" (
  echo [StAgent] Using local source tree; skipping git clone.
) else if "!FLAG_DRYRUN!"=="1" (
  echo [StAgent] [dry-run] would clone/update: !StAgent_GIT_URL! -^> !StAgent_CLONE_DIR! @ !StAgent_REF!
) else (
  if exist "!StAgent_CLONE_DIR!\.git" (
    echo [StAgent] Updating existing repo at !StAgent_CLONE_DIR! ...
    set "_RETRY_COUNT=0"
    :fetch_retry
    git -C "!StAgent_CLONE_DIR!" fetch --depth 1 origin "!StAgent_REF!" 2>nul
    if errorlevel 1 (
      set /a "_RETRY_COUNT+=1"
      if !_RETRY_COUNT! lss !StAgent_INSTALL_RETRIES! (
        echo [StAgent] Fetch attempt !_RETRY_COUNT! failed, retrying...
        timeout /t 2 /nobreak >nul
        goto :fetch_retry
      )
      git -C "!StAgent_CLONE_DIR!" fetch origin
    )
    git -C "!StAgent_CLONE_DIR!" checkout "!StAgent_REF!"
    if errorlevel 1 (
      echo [StAgent] git checkout failed for ref: !StAgent_REF! 1>&2
      exit /b 1
    )
    git -C "!StAgent_CLONE_DIR!" pull --ff-only 2>nul
  ) else if exist "!StAgent_CLONE_DIR!" (
    echo [StAgent] Path exists and is not a git repo: !StAgent_CLONE_DIR! 1>&2
    exit /b 1
  ) else (
    echo [StAgent] Cloning !StAgent_GIT_URL! -^> !StAgent_CLONE_DIR! ^(ref: !StAgent_REF!^) ...
    set "_RETRY_COUNT=0"
    :clone_retry
    git clone --depth 1 --branch "!StAgent_REF!" "!StAgent_GIT_URL!" "!StAgent_CLONE_DIR!"
    if errorlevel 1 (
      set /a "_RETRY_COUNT+=1"
      if !_RETRY_COUNT! lss !StAgent_INSTALL_RETRIES! (
        echo [StAgent] Clone attempt !_RETRY_COUNT! failed, retrying...
        if exist "!StAgent_CLONE_DIR!" rmdir /s /q "!StAgent_CLONE_DIR!" 2>nul
        timeout /t 2 /nobreak >nul
        goto :clone_retry
      )
      echo [StAgent] Shallow clone failed; trying full clone ...
      if exist "!StAgent_CLONE_DIR!" rmdir /s /q "!StAgent_CLONE_DIR!" 2>nul
      git clone "!StAgent_GIT_URL!" "!StAgent_CLONE_DIR!"
      if errorlevel 1 (
        echo [StAgent] git clone failed 1>&2
        exit /b 1
      )
      git -C "!StAgent_CLONE_DIR!" checkout "!StAgent_REF!"
      if errorlevel 1 (
        echo [StAgent] git checkout failed for ref: !StAgent_REF! 1>&2
        exit /b 1
      )
    )
  )
)

if "!FLAG_DRYRUN!"=="1" (
  echo [StAgent] [dry-run] done.
  exit /b 0
)

if not exist "!StAgent_CLONE_DIR!\start.sh" (
  echo [StAgent] start.sh not found in: !StAgent_CLONE_DIR! 1>&2
  exit /b 1
)

set "BASH_EXE="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH_EXE if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH_EXE=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined BASH_EXE if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "BASH_EXE=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if not defined BASH_EXE (
  for /f "delims=" %%G in ('where git 2^>nul') do (
    for %%H in ("%%~dpG..") do (
      if exist "%%~fH\bin\bash.exe" set "BASH_EXE=%%~fH\bin\bash.exe"
    )
    goto :bash_search_done
  )
)
:bash_search_done
if not defined BASH_EXE (
  echo [StAgent] Git Bash not found. Install Git for Windows. 1>&2
  exit /b 1
)

REM Git Bash cd path: C:\Users\... -> /C/Users/...
set "FD=!StAgent_CLONE_DIR!"
for %%I in ("!FD!") do set "FD=%%~fI"
set "DRIVE=!FD:~0,1!"
set "REST=!FD:~3!"
set "REST=!REST:\=/!"
set "UNIX_DIR=/!DRIVE!/!REST!"

REM Properly quote UV_SYNC_EXTRAS in the bash export.
set "SAFE_EXTRAS=!UV_SYNC_EXTRAS:'='\''!"
set "SAFE_INDEX=!UV_INDEX_URL:'='\''!"
set "BASH_PREFIX=export UV_SYNC_EXTRAS='!SAFE_EXTRAS!' UV_INDEX_URL='!SAFE_INDEX!' UV_VENV_CLEAR='!UV_VENV_CLEAR!'; "

if not "!FLAG_SKIPINIT!"=="1" (
  if "!FLAG_DRYRUN!"=="0" (
    echo [StAgent] Running ./start.sh sync-python and StAgent init --defaults ...
    "!BASH_EXE!" -lc "!BASH_PREFIX!cd '!UNIX_DIR!' && { chmod +x start.sh 2>/dev/null || true; ./start.sh sync-python && uv run --directory backend StAgent init --defaults; }"
    if errorlevel 1 (
      echo [StAgent] sync-python / StAgent init failed 1>&2
      exit /b 1
    )
  )
)

if "!FLAG_NOCHECK!"=="0" if "!FLAG_SKIPSTART!"=="0" (
  echo [StAgent] Running ./start.sh check ...
  "!BASH_EXE!" -lc "!BASH_PREFIX!cd '!UNIX_DIR!' && { chmod +x start.sh 2>/dev/null || true; ./start.sh check; }"
  if errorlevel 1 (
    echo [StAgent] ./start.sh check failed 1>&2
    exit /b 1
  )
)

if "!FLAG_SKIPSTART!"=="1" (
  echo [StAgent] Skip start — not launching services.
  echo.
  echo StAgent install finished.
  echo   Directory: !StAgent_CLONE_DIR!
  echo.
  echo [StAgent] Next: cd /d "!StAgent_CLONE_DIR!" ^& start.sh   ^(via Git Bash: ./start.sh^)
  exit /b 0
)

echo [StAgent] Starting StAgent ^(./start.sh all^) ...
"!BASH_EXE!" -lc "!BASH_PREFIX!cd '!UNIX_DIR!' && exec ./start.sh all"
exit /b %ERRORLEVEL%

:help
echo StAgent Windows installer ^(CMD^)
echo.
echo Usage: install.bat [options]
echo   -GitUrl ^<url^>        Git clone URL
echo   -Dir ^<path^>          Install directory ^(default: %%USERPROFILE%%\StAgent-desktop^)
echo   -Ref ^<ref^>           Branch or tag ^(default: main^)
echo   -Version ^<ref^>       Same as -Ref
echo   -FromSource ^<path^>   Use existing checkout ^(skips git clone^)
echo   -Extras ^<names^>     uv sync extras ^(default: dev browser^)
echo   -SkipStart            Do not run start.sh
echo   -SkipInit             Skip sync-python + StAgent init --defaults
echo   -DryRun               Print planned steps only
echo   -NoCheck              Skip start.sh check
echo   -Help /?
echo.
echo Environment: StAgent_GIT_URL StAgent_CLONE_DIR StAgent_REF StAgent_SKIP_START
echo   StAgent_DRY_RUN StAgent_RUN_CHECK StAgent_SKIP_INIT StAgent_INSTALL_RETRIES
echo   UV_SYNC_EXTRAS UV_INDEX_URL UV_VENV_CLEAR
echo.
echo Examples:
echo   curl -fsSL https://vixues.com.cn/install.bat -o install.bat ^&^& install.bat
echo   scripts\install.bat -SkipStart
exit /b 0
