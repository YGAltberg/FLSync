@echo off
rem Starts FLSync: finds a Python (the newest FL Studio's own first, then
rem "py -3", then "python") and runs app\flsync.py with the arguments given.
rem Setup copies this file to %LOCALAPPDATA%\FLSync\app, and the shortcuts
rem "FLSync" on the desktop and in the Start menu run that copy. Looking for
rem Python each time keeps it working when FL Studio is updated or removed.
rem Paths here can hold spaces and parentheses ("Program Files (x86)"), so
rem they stay in quotes and never inside ( ) blocks.
setlocal
for /f "tokens=2 delims=:." %%C in ('chcp') do set "FLSYNC_CP=%%C"
chcp 65001 >nul
title FLSync
if not exist "%~dp0app\flsync.py" goto no_app
call :find_python
if not defined PY goto no_python
"%PY%" %PYARGS% "%~dp0app\flsync.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
goto end

:no_app
echo.
echo FLSync saknas: hittar inte "%~dp0app\flsync.py".
echo Kör Installera FLSync.cmd igen.
echo.
pause
set "CODE=1"
goto end

:no_python
echo.
echo Hittar ingen Python att köra FLSync med. FLSync letar efter
echo FL Studios egen Python, Image-Line\FL Studio ...\Shared\Python\python.exe
echo under Program Files, och efter Python 3.10 eller senare på datorn.
echo Installera FL Studio 2024 eller senare, eller Python från
echo https://www.python.org/downloads/ och starta sedan FLSync igen.
echo.
pause
set "CODE=1"

:end
if defined FLSYNC_CP chcp %FLSYNC_CP% >nul
exit /b %CODE%

:find_python
rem The newest FL Studio's own Python, else a Python 3.10+ on the computer.
set "PY="
set "PYARGS="
set "PYVER=-1"
call :scan "%ProgramW6432%"
call :scan "%ProgramFiles%"
call :scan "%ProgramFiles(x86)%"
rem FL installed somewhere else (another drive): where FL itself says it is.
for /f "tokens=3,*" %%A in ('reg query "HKCU\Software\Image-Line\Shared\Paths" /v "Install path" 2^>nul ^| findstr /c:"REG_SZ"') do call :consider "%%~B"
if defined PY goto :eof
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py" && set "PYARGS=-3"
if defined PY goto :eof
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
goto :eof

:scan
rem %1: a Program Files folder; looks at every Image-Line\FL Studio * in it.
if "%~1"=="" goto :eof
if not exist "%~1\Image-Line\" goto :eof
pushd "%~1\Image-Line"
for /d %%D in ("FL Studio *") do call :consider "%%~fD"
popd
goto :eof

:consider
rem %1: an FL Studio folder. Kept if it has a Python and a higher number than
rem the one found so far ("FL Studio 2025" beats "FL Studio 21").
if not exist "%~1\Shared\Python\python.exe" goto :eof
set "FLNAME=%~nx1"
set "FLNUM=0"
set /a "FLNUM=%FLNAME:FL Studio =%" >nul 2>nul
if %FLNUM% LEQ %PYVER% goto :eof
set "PY=%~1\Shared\Python\python.exe"
set "PYVER=%FLNUM%"
goto :eof
