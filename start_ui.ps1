$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
if (-not $env:MINERU_DEVICE_MODE) {
    $env:MINERU_DEVICE_MODE = "cpu"
}
python -m uvicorn rapid_doc_ui.app:app --host 127.0.0.1 --port 7862

