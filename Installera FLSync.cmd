@echo off
rem Installs FLSync: finds a Python (the newest FL Studio's own first, then
rem "py -3", then "python") and runs setup\setup.py with the arguments given:
rem --dry-run, --uninstall, --yes, --skip-loopmidi, --skip-fl-config.
rem Paths here can hold spaces and parentheses ("Program Files (x86)"), so
rem they stay in quotes and never inside ( ) blocks.
setlocal
for /f "tokens=2 delims=:." %%C in ('chcp') do set "FLSYNC_CP=%%C"
chcp 65001 >nul
title FLSync - installation
rem Out of this folder: an uninstall may remove it (setlocal brings us back).
cd /d "%USERPROFILE%"
if not exist "%~dp0setup\setup.py" goto no_setup
call :find_python
if not defined PY goto no_python
"%PY%" %PYARGS% "%~dp0setup\setup.py" %*
set "CODE=%ERRORLEVEL%"
echo.
pause
goto end

:no_setup
rem E.g. double-clicked inside the zip file: Windows unpacked only this file.
echo.
echo Hittar inte resten av FLSync bredvid den här filen. Packa upp hela FLSync
echo först: högerklicka på zip-filen och välj Extrahera alla. Dubbelklicka sedan
echo på Installera FLSync.cmd i den uppackade mappen.
echo.
pause
set "CODE=1"
goto end

:no_python
echo.
echo Hittar ingen Python att köra installationen med. FLSync letar efter
echo FL Studios egen Python, Image-Line\FL Studio ...\Shared\Python\python.exe
echo under Program Files, och efter Python 3.10 eller senare på datorn.
echo Installera FL Studio 2024 eller senare, eller Python från
echo https://www.python.org/downloads/ och kör sedan den här filen igen.
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
