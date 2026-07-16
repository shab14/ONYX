# Fix ONYX wake word + opencv

## Le problème

```
WARNING [Voice] Wake word indisponible (No module named 'openwakeword')
[Claude] opencv-python absent
```

Les deps sont dans `requirements.txt` mais pas installées dans ton venv actuel.

## Fix rapide (1 commande)

```powershell
# venv activé
pip install openwakeword onnxruntime opencv-python
```

Puis premier lancement, openwakeword télécharge ses modèles (~30 Mo, auto).

## OU script tout-en-un

Lance `install_missing.ps1` (joint) :
```powershell
.\install_missing.ps1
```

Il installe + télécharge les modèles + teste que tout marche.

## Après l'install

```powershell
python gui.py
```

Tu dois voir dans les logs :
```
[Voice] Wake word « hey_jarvis » prêt
```

Au lieu du WARNING.

## Fichiers livrés cette fois

- **gui.py** = INCHANGÉ (canvas tkinter natif, comme avant) ✓
- **config.py** = ajout `BARGE_IN_REQUIRE_PRE_SILENCE` + `WAKE_WORD_CUSTOM_MODEL`
- **voice_mode.py** = barge-in v3 (anti-faux-positif) + support wake custom .onnx
- **install_missing.ps1** = fix deps

Zéro HTML, zéro Edge, zéro Rive. Tout en Python natif.
