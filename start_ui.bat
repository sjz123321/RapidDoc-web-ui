@echo off
cd /d "%~dp0"
if "%MINERU_DEVICE_MODE%"=="" set MINERU_DEVICE_MODE=cpu
python -m uvicorn rapid_doc_ui.app:app --host 127.0.0.1 --port 7862

