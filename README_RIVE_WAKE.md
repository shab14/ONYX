# ONYX — Rive Avatar + Wake Word + Barge-in (v3)

3 nouveautés. Tout marche **out-of-the-box** avec les fichiers actuels.
Setup optionnel détaillé ci-dessous.

---

## 1. Wake word « Hey ONYX »  ✅ DÉJÀ ACTIF

**Statut actuel** : utilise le modèle pré-entraîné `hey_jarvis` (il n'existe pas de
modèle `onyx` officiel).

```python
# config.py
WAKE_WORD_ENABLED  = True
WAKE_WORD_NAME     = "hey_jarvis"   # ← change pour "alexa", "hey_mycroft"…
WAKE_WORD_THRESHOLD = 0.5            # 0=sensible, 1=strict
```

Au premier démarrage du mode vocal, `openwakeword` télécharge ses modèles
(~30 Mo, automatique).

### Custom « Hey ONYX » entraîné maison

Pour un VRAI « Hey ONYX » :

1. Train un modèle via le notebook officiel openwakeword :
   https://github.com/dscripka/openWakeWord#training-custom-models
2. Exporte le `.onnx` (ex: `hey_onyx.onnx`)
3. Pose-le dans `C:\Users\shabd\ONYX\models\hey_onyx.onnx`
4. Edite `config.py` :
   ```python
   WAKE_WORD_CUSTOM_MODEL = r"C:\Users\shabd\ONYX\models\hey_onyx.onnx"
   ```

Le modèle custom prend priorité sur `WAKE_WORD_NAME`.

---

## 2. Barge-in (couper ONYX en parlant)  ✅ DÉJÀ ACTIF — v3 amélioré

Tu peux parler par-dessus ONYX → il se tait et écoute.

**v3 anti-faux-positif** : exige une période de silence AVANT le burst de voix.
Évite les coupures fantômes dues à l'écho TTS, pops, clics clavier.

```python
# config.py
BARGE_IN_ENABLED  = True
BARGE_IN_RMS      = 0.045   # ↓ = plus sensible (coupe plus vite)
BARGE_IN_FRAMES   = 4       # frames de voix consécutives avant coupure (~50ms/frame)
BARGE_IN_REQUIRE_PRE_SILENCE = 3   # 0=désactive l'anti-faux-positif
```

**Important** : le barge-in ne fonctionne QUE si Piper TTS est configuré
(`PIPER_MODEL_PATH` dans config.py). pyttsx3 est bloquant — non interruptible.

### Setup Piper TTS (voix naturelle + barge-in actif)

1. Télécharge une voix FR :
   https://huggingface.co/rhasspy/piper-voices/tree/main/fr/fr_FR
   Recommandé : `fr_FR-siwis-medium.onnx` (~60 Mo) + son `.json` config
2. Pose les 2 fichiers dans `C:\Users\shabd\ONYX\voices\`
3. Edite `config.py` :
   ```python
   PIPER_MODEL_PATH = r"C:\Users\shabd\ONYX\voices\fr_FR-siwis-medium.onnx"
   ```

---

## 3. Rive Avatar  🆕 NOUVEAU

Avatar Rive animé en fenêtre Edge à la place du canvas tkinter.
Fallback automatique sur le canvas si Rive KO.

### Architecture

```
gui.py → overlay_factory.make_overlay()
   ├─ tente RiveOverlay (rive_overlay.py)
   │    ├─ mini HTTP server 127.0.0.1:8101 (stdlib, daemon thread)
   │    │   ├─ GET /         → HTML + Rive runtime + SSE client
   │    │   ├─ GET /avatar.riv → ton fichier .riv
   │    │   └─ GET /events    → SSE stream (état temps réel)
   │    └─ subprocess msedge --app=http://127.0.0.1:8101/
   └─ fallback VocalOverlay (canvas tkinter) si Rive échoue
```

### Setup

#### A) Quick test (sans .riv — fallback CSS animé)

Le HTML embarqué a un **fallback CSS** : sans `.riv`, tu vois un orbe pulsant
qui change de couleur selon l'état. Pour tester :

```bash
python rive_overlay.py
```

Edge s'ouvre, cycle des états toutes les 2.5s. Ctrl+C pour quitter.

#### B) Vrai avatar Rive

1. **Crée ton avatar dans Rive Editor** (gratuit) : https://editor.rive.app/

2. **Setup obligatoire dans Rive** :
   - Une **State Machine** nommée `OnyxStateMachine`
   - Un **Number Input** nommé `state` avec mapping :
     ```
     0 = OFF
     1 = LISTENING       (vert, idle posé)
     2 = THINKING        (jaune, réflexion / tourne)
     3 = SPEAKING        (orange, parle / bouche/pulse)
     4 = MUTED           (rouge, croix / dormant)
     5 = WAITING_WAKE    (cyan, respire lentement)
     ```
   - Crée 6 états dans la state machine, chacun déclenché par `state == N`

3. **Export** : `File → Export → Runtime (.riv)`

4. **Place le fichier** :
   ```
   C:\Users\shabd\ONYX\avatar\onyx.riv
   ```
   (ou change `RIVE_FILE` dans `config.py`)

5. **Lance ONYX vocal** — le `.riv` sera chargé automatiquement.

#### C) Désactiver Rive (forcer canvas tkinter)

```python
# config.py
RIVE_ENABLED = False
```

### Customisation

```python
# config.py
RIVE_FILE          = USER_HOME / "ONYX" / "avatar" / "onyx.riv"
RIVE_OVERLAY_PORT  = 8101         # change si conflit
RIVE_WINDOW_SIZE   = (380, 480)   # (largeur, hauteur)
RIVE_EDGE_PATH     = ""           # chemin custom Edge ; vide = auto
RIVE_STATE_MACHINE = "OnyxStateMachine"
RIVE_STATE_INPUT   = "state"
```

### Inspiration pour l'avatar

Style ONYX (cohérent palette MARK XL) :
- **Couleur primaire** : cyan `#00d4ff`
- **Background** : noir profond `#00060a`
- **Accents** : vert `#00ff88` / orange `#ff6b00` / jaune `#ffcc00` / rouge `#ff3366`
- Format conseillé : **640×640 px**, transparent, vectoriel

Idées de design :
- Cube hexagonal qui tourne et change de couleur
- Visage stylisé minimaliste (yeux + bouche → bouche bouge en SPEAKING)
- Onde sinusoïdale qui pulse
- Réacteur Iron Man arc reactor style

---

## Debug

### Wake word

```powershell
# Logs dans %APPDATA%\ONYX\onyx.log
type "$env:APPDATA\ONYX\onyx.log" | Select-String "Wake"
```

Cherche : `[Voice] Wake word « hey_jarvis » prêt`

### Rive overlay

```powershell
# Test standalone
python rive_overlay.py
```

Vérifie dans la fenêtre Edge ouverte :
- F12 → Console
- Tu dois voir `[ONYX] Rive OK OnyxStateMachine` si .riv chargé
- Sinon `Rive load error: ...` ou `Rive runtime absent → fallback CSS`

### Barge-in

```powershell
type "$env:APPDATA\ONYX\onyx.log" | Select-String "Barge"
```

Si trop sensible : ↑ `BARGE_IN_RMS` (essaie 0.06)
Si pas assez : ↓ `BARGE_IN_RMS` (essaie 0.03), ↓ `BARGE_IN_FRAMES` (essaie 2)

---

## Récap fichiers modifiés/créés

```
ONYX/
├── config.py             ← v3 (Rive config + barge-in pre-silence + wake custom)
├── voice_mode.py         ← v6.1 (barge-in v3 + wake custom path)
├── gui.py                ← utilise overlay_factory au lieu de vocal_overlay direct
├── rive_overlay.py       ← NOUVEAU — HTTP+SSE+Edge
├── overlay_factory.py    ← NOUVEAU — choisit Rive ou tkinter
├── vocal_overlay.py      ← INCHANGÉ (utilisé en fallback)
└── avatar/
    └── onyx.riv          ← À créer dans Rive Editor
```

Aucun nouveau package pip requis (zero dep externe pour Rive : HTTP stdlib + Edge).
