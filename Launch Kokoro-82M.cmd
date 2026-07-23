@echo off
set "KOKORO_HOME=%~dp0"
set "PATH=%KOKORO_HOME%python;%KOKORO_HOME%python\Scripts;%PATH%"
set "PYTHONUTF8=1"
"%KOKORO_HOME%python\pythonw.exe" "%KOKORO_HOME%kokoro_gui.py"
