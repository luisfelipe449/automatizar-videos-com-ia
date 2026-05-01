@echo off
echo Parando servidor antigo...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq uvicorn*" 2>nul
timeout /t 2 /nobreak >nul

echo Iniciando dashboard...
python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --reload

pause
