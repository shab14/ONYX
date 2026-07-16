<div align="center">

# 🧠 ONYX
### Orchestrated Neural eXecution sYstem

**Assistant personnel local pour Windows, piloté par un LLM auto-hébergé.**
Zéro cloud pour le cœur du système · Interface bureau · Mode vocal · Contrôle à distance WiFi

![Python](https://img.shields.io/badge/python-3.14-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Ollama%20(local)-black?logo=ollama&logoColor=white)
![License](https://img.shields.io/badge/status-perso%20%2F%20non%20publié-lightgrey)

</div>

---

## ⚠️ À faire avant/après avoir cloné ce dépôt

> Ce dépôt est **public**. Deux choses à corriger avant utilisation :

| Action | Pourquoi | Comment |
|---|---|---|
| 🔑 **Changer le PIN par défaut** | `auth.py` contient un hash legacy (`_LEGACY_PIN_HASH`) avec un **sel codé en dur** dans le code — donc public. Le PIN doit être considéré comme compromis. | `python auth.py set <nouveau_pin>` → génère des credentials avec sel aléatoire, stockés hors du code dans `AppData/Roaming/ONYX/credentials.json` |
| 📁 **Adapter les chemins codés en dur** | `config.py` et `README_RIVE_WAKE.md` contiennent des exemples type `C:\Users\<user>\ONYX\...` | Remplace `<user>` par ton propre nom d'utilisateur Windows |

---

## ✨ Fonctionnalités

- 🖥️ **Interface bureau** (`gui.py`) — canvas `customtkinter` natif, écran PIN au démarrage
- 🧩 **Backend LLM local** (`main.py`) — via Ollama (`deepseek-r1:7b` par défaut), aucune donnée envoyée dans le cloud
- 🎙️ **Mode vocal** (`voice_mode.py`, `vocal_overlay.py`) — wake word offline *"Hey ONYX"* (`openwakeword`), transcription Whisper, barge-in anti-faux-positif
- 📡 **Serveur WiFi** (`server.py`) — API FastAPI avec auth par PIN/session token, pairing par QR code (`qr_popup.py`) pour piloter ONYX depuis un téléphone sur le même réseau local
- ⏰ **Rappels & planification** (`reminders.py`, `scheduler.py`) — notifications, TTS, tâches récurrentes
- 🧠 **Mémoire persistante** (`memory_manager.py`) — contexte utilisateur injecté dans le prompt système, élagage/fusion intelligents
- 🛠️ **Skills auto-évolutifs** (`skill_forge.py`, `skills_engine.py`, `skills_dynamic.py`) — création et exécution de skills multi-étapes
- 👁️ **Vision** (`vision.py`) — OCR (Tesseract), analyse de captures d'écran
- ⚙️ **Actions système** (`actions.py`, `shortcuts.py`) — volume, luminosité, fenêtres, presse-papiers, corbeille...
- 🌐 **Intégrations** — recherche web (DDG), météo, résumé YouTube, envoi de messages (WhatsApp/Telegram/Discord/Signal)

---

## 📋 Prérequis

- Windows
- Python 3.14
- [Ollama](https://ollama.com) installé avec un modèle chargé (ex. `ollama pull deepseek-r1:7b`)
- Tesseract OCR (`C:\Program Files\Tesseract-OCR\tesseract.exe`)
- ffmpeg dans le PATH (pour l'enregistrement écran)

## 🚀 Installation

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Dépendances optionnelles (wake word, TTS naturel, drag & drop) : voir `install_missing.ps1` ou le bas de `requirements.txt`.

## ▶️ Lancement

```powershell
python gui.py
```

Le serveur WiFi (`server.py`) peut être lancé séparément pour un contrôle à distance depuis le même réseau local.

## 🔧 Configuration

Toute la config centralisée est dans `config.py` (modèle LLM, timeouts, chemins de logs/captures, paramètres du wake word, etc.). Voir aussi `FIX_README.md` et `INTEGRATION_OPENJARVIS.md` pour des notes d'installation et d'intégration spécifiques.

## 📂 Structure du projet

```
ONYX/
├── main.py              # backend : routage, appel LLM, état de conversation
├── gui.py                # interface bureau
├── server.py              # serveur WiFi (FastAPI) + auth
├── auth.py                # PIN / session tokens
├── config.py               # config centralisée
├── actions.py              # actions système
├── shortcuts.py            # raccourcis
├── voice_mode.py / vocal_overlay.py   # mode vocal
├── reminders.py / scheduler.py        # rappels & tâches planifiées
├── memory_manager.py        # mémoire persistante
├── skill_forge.py / skills_engine.py / skills_dynamic.py  # skills auto-évolutifs
├── vision.py                # OCR / analyse écran
├── rive_overlay.py / overlay_factory.py / qr_popup.py     # UI additionnelle
└── templates/                # assets UI
```

## 🔒 Confidentialité

ONYX tourne entièrement en local (LLM via Ollama, pas d'API cloud pour le cœur du système). Le fichier `.env` et les logs sont exclus du dépôt via `.gitignore`.

---

<div align="center">
<sub>Projet personnel — pas destiné à la distribution publique tel quel.</sub>
</div>
