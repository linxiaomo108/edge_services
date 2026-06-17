@echo off
setlocal
cd /d "%~dp0"

set "EDGE_ROOT=%~dp0"
set "EDGE_HIK_SDK_DIR=%EDGE_ROOT%sdk\download"
set "EDGE_HIK_SYSTRANS_SDK_DIR=%EDGE_ROOT%sdk\systrans"
set "EDGE_FFMPEG_BIN=%EDGE_ROOT%ffmpeg\ffmpeg.exe"
set "PATH=%EDGE_ROOT%sdk\download;%EDGE_ROOT%sdk\download\ClientDemoDll;%EDGE_ROOT%sdk\systrans;%EDGE_ROOT%ffmpeg;%PATH%"

if defined DIY_PYTHON_EXE (
  set "PYTHON_EXE=%DIY_PYTHON_EXE%"
) else if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
) else (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" -u diy_download.py
endlocal
