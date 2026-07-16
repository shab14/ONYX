# install_missing.ps1 — Fix deps ONYX manquantes
# Wake word + opencv (template matching Claude)
# Usage : .\install_missing.ps1  depuis venv activé

Write-Host "ONYX deps fix" -ForegroundColor Cyan
Write-Host "=============" -ForegroundColor Cyan

# Vérif venv actif
if (-not $env:VIRTUAL_ENV) {
    Write-Host "ERREUR : venv pas actif. Fais d'abord :" -ForegroundColor Red
    Write-Host "  venv\Scripts\activate" -ForegroundColor Yellow
    exit 1
}
Write-Host "venv : $env:VIRTUAL_ENV" -ForegroundColor Green

# 1. opencv (Claude template matching)
Write-Host "`n[1/3] opencv-python..." -ForegroundColor Cyan
pip install opencv-python

# 2. openwakeword (wake word "Hey ONYX")
Write-Host "`n[2/3] openwakeword + onnxruntime..." -ForegroundColor Cyan
# tflite-runtime souvent buggé sur Windows/3.14 → force onnxruntime backend
pip install openwakeword onnxruntime

# 3. Force re-download des modèles openwakeword (~30 Mo)
Write-Host "`n[3/3] Téléchargement modèles wake word..." -ForegroundColor Cyan
python -c "from openwakeword.utils import download_models; download_models(); print('OK')"

Write-Host "`n=============" -ForegroundColor Cyan
Write-Host "Test wake word :" -ForegroundColor Green
python -c @"
try:
    from openwakeword.model import Model
    m = Model(wakeword_models=['hey_jarvis'])
    print('Wake word OK — hey_jarvis prêt')
except Exception as e:
    print(f'KO : {e}')
"@

Write-Host "`nTest opencv :" -ForegroundColor Green
python -c "import cv2; print(f'opencv OK — version {cv2.__version__}')"

Write-Host "`nFini. Relance : python gui.py" -ForegroundColor Cyan
