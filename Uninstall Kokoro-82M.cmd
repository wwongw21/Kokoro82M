@echo off
set "TARGET=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d 'Kokoro-82M.lnk'); $s=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Kokoro-82M.lnk'; Remove-Item -Force -ErrorAction SilentlyContinue $s"
start "" cmd /c "timeout /t 2 /nobreak >nul & rmdir /s /q ^"%TARGET%^""
exit
