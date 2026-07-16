"""
gui.py — ONYX · Interface principale avec écran PIN au démarrage
"""
from __future__ import annotations

import math
import threading
from datetime import datetime

import customtkinter as ctk
import tkinter as tk

try:
    from auth import verify_pin
    AUTH_OK = True
except ImportError:
    AUTH_OK = False

try:
    from qr_popup import QRPopup
    QR_OK = True
except ImportError:
    QR_OK = False

try:
    from main import chat_llm, extraire_latence, router
except ImportError:
    def router(txt): return None
    def chat_llm(txt): return "(backend manquant)"
    def extraire_latence(r): return r, None

try:
    from voice_mode import VoiceMode
    VOICE_OK = True
except ImportError:
    VOICE_OK = False

try:
    from vocal_overlay import VocalOverlay
    OVERLAY_OK = True
except ImportError:
    OVERLAY_OK = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_OK = True
except ImportError:
    DND_OK = False

try:
    from shortcuts import COLOR_HEX, SHORTCUTS
    _SHORTCUTS_OK = True
except ImportError:
    _SHORTCUTS_OK = False
    SHORTCUTS, COLOR_HEX = [], {}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

BG      = "#0c0c0c"
BG2     = "#111111"
SURFACE = "#181818"
BORDER  = "#2a2a2a"
TEXT    = "#e8e8e8"
TEXT2   = "#888888"
TEXT3   = "#444444"
GREEN   = "#4ade80"
BLUE    = "#60a5fa"
ORANGE  = "#fb923c"
PURPLE  = "#a78bfa"
RED     = "#f87171"
CYAN    = "#22d3ee"

MAX_PIN_ATTEMPTS = 5


# ── Logo hexagone ─────────────────────────────────────────────────────────────
class HexLogo(tk.Canvas):
    def __init__(self, master: tk.Misc, size: int = 52, **kw: object) -> None:
        super().__init__(master, width=size, height=size,
                         bg=BG, highlightthickness=0, **kw)
        self._size = size
        self._draw()

    def _hex_points(self, cx, cy, r):
        pts = []
        for i in range(6):
            a = math.radians(90 + i * 60)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    def _draw(self):
        s = self._size; cx = cy = s / 2
        r_out = s * 0.44; r_in = s * 0.28; r_dot = s * 0.08
        self.create_polygon(self._hex_points(cx, cy, r_out),
                            outline=TEXT, width=1.4, fill="")
        self.create_polygon(self._hex_points(cx, cy, r_in),
                            outline=TEXT2, width=0.9, fill="")
        for i in range(6):
            a = math.radians(90 + i * 60)
            self.create_line(cx + r_in * math.cos(a), cy + r_in * math.sin(a),
                             cx + r_out * math.cos(a), cy + r_out * math.sin(a),
                             fill=TEXT2, width=0.8)
        self.create_oval(cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot,
                         fill=TEXT, outline="")


# ── Écran PIN ─────────────────────────────────────────────────────────────────
class PINScreen(ctk.CTkFrame):
    def __init__(self, master: "ONYXApp") -> None:
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._attempts = 0
        self._locked   = False
        self._build()

    def _build(self) -> None:
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(7, weight=1)
        self.grid_columnconfigure(0, weight=1)

        HexLogo(self, size=52).grid(row=1, column=0, pady=(0, 10))

        ctk.CTkLabel(self, text="ONYX",
                     font=ctk.CTkFont("Courier New", 32, "bold"),
                     text_color=TEXT).grid(row=2, column=0)

        ctk.CTkLabel(self, text="Entrez votre code PIN",
                     font=ctk.CTkFont("Courier New", 13),
                     text_color=TEXT2).grid(row=3, column=0, pady=(6, 20))

        self._pin_var = ctk.StringVar()
        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._pin_var,
            width=180, height=48,
            corner_radius=8,
            border_color=BORDER,
            fg_color=SURFACE,
            text_color=TEXT,
            font=ctk.CTkFont("Courier New", 22, "bold"),
            justify="center",
            show="●",
        )
        self._entry.grid(row=4, column=0, pady=(0, 14))
        self._entry.bind("<Return>", lambda _: self._check())
        self._entry.focus()

        ctk.CTkButton(
            self, text="Déverrouiller",
            width=180, height=40,
            corner_radius=8,
            fg_color=SURFACE,
            hover_color="#1e1e1e",
            border_width=1, border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont("Courier New", 13, "bold"),
            command=self._check,
        ).grid(row=5, column=0)

        self._msg = ctk.CTkLabel(self, text="",
                                  font=ctk.CTkFont("Courier New", 11),
                                  text_color=RED)
        self._msg.grid(row=6, column=0, pady=(14, 0))

    def _check(self) -> None:
        if self._locked:
            return
        pin = self._pin_var.get().strip()
        self._pin_var.set("")

        if not AUTH_OK:
            self.destroy()
            return

        if verify_pin(pin):
            self.destroy()
            return

        self._attempts += 1
        remaining = MAX_PIN_ATTEMPTS - self._attempts

        if remaining <= 0:
            self._locked = True
            self._entry.configure(state="disabled")
            self._msg.configure(
                text="Trop de tentatives. Relance ONYX.",
                text_color=RED,
            )
        else:
            self._msg.configure(
                text=f"Code incorrect. {remaining} essai(s) restant(s).",
                text_color=ORANGE,
            )
            self._entry.focus()


# ── App principale ────────────────────────────────────────────────────────────
class ONYXApp(ctk.CTk):

    # Source unique partagée avec le mobile (shortcuts.py). color_key → hex desktop.
    _ALL_SHORTCUTS = [
        (label, fill, COLOR_HEX.get(ck, TEXT2), cat)
        for (label, fill, ck, cat) in SHORTCUTS
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title("ONYX")
        self.geometry("820x660")
        self.minsize(640, 500)
        self.configure(fg_color=BG)
        self._thinking          = False
        self._shortcuts_visible = False
        self._shortcuts_panel   = None
        self._server_proc: list = []
        self._qr_popup          = None
        self._voice_mode: "VoiceMode | None" = None
        self._voice_active: bool = False
        self._vocal_overlay: "VocalOverlay | None" = None
        self._build()
        self._setup_dnd()
        PINScreen(self)

    def _setup_dnd(self) -> None:
        """Drag & drop de fichiers → menu d'actions. No-op si tkinterdnd2 absent."""
        if not DND_OK:
            return
        try:
            # CTk n'hérite pas de TkinterDnD : on charge le package tkdnd sur cette fenêtre
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("[GUI] DnD indisponible : %s", exc)

    def _on_drop(self, event) -> None:
        # event.data = "{C:\path un.txt} {C:\path deux.png}" (accolades si espaces)
        import re as _re
        paths = _re.findall(r"\{([^}]*)\}|(\S+)", event.data)
        files = [a or b for a, b in paths if (a or b)]
        if not files:
            return
        self._dropped_files = files
        self._show_drop_menu(files)

    def _show_drop_menu(self, files: list[str]) -> None:
        from pathlib import Path as _P
        noms = ", ".join(_P(f).name for f in files[:3])
        if len(files) > 3:
            noms += f" +{len(files) - 3}"
        self._log("toi", f"📎 {len(files)} fichier(s) : {noms}", "user")

        menu = ctk.CTkFrame(self, fg_color=BG2, border_color=BORDER, border_width=1, corner_radius=8)
        menu.grid(row=2, column=0, sticky="ew", padx=28, pady=(8, 0))

        actions = [
            ("👁 Analyser (OCR+IA)", lambda: self._drop_action("ocr")),
            ("📂 Ouvrir",           lambda: self._drop_action("open")),
            ("📦 Zipper",           lambda: self._drop_action("zip")),
            ("⚖️ Taille",           lambda: self._drop_action("size")),
            ("✕ Annuler",           lambda: menu.destroy()),
        ]
        self._drop_menu = menu
        row = ctk.CTkFrame(menu, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=10)
        for label, cmd in actions:
            ctk.CTkButton(
                row, text=label, height=30, corner_radius=5,
                border_width=1, border_color=BORDER,
                fg_color="transparent", hover_color=SURFACE,
                text_color=CYAN if "Analyser" in label else TEXT2,
                font=ctk.CTkFont("Courier New", 12),
                command=cmd,
            ).pack(side="left", padx=4)

    def _drop_action(self, kind: str) -> None:
        files = getattr(self, "_dropped_files", [])
        if hasattr(self, "_drop_menu") and self._drop_menu:
            self._drop_menu.destroy()
            self._drop_menu = None
        if not files:
            return
        cmd_map = {
            "ocr":  "aide moi, analyse ce fichier : ",
            "open": "ouvre le fichier ",
            "zip":  "zippe ",
            "size": "taille de ",
        }
        prefix = cmd_map.get(kind, "")
        for f in files:
            full = f'{prefix}"{f}"'
            self._log("toi", full, "user")
            self._set_thinking(True)
            threading.Thread(target=self._process, args=(full,), daemon=True).start()

    # ── Layout ────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_header()
        self._build_shortcut_bar()
        self._build_chat()
        self._build_input()

    # ── Header ────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color="transparent", height=70)
        hdr.grid(row=0, column=0, sticky="ew", padx=28, pady=(22, 0))
        hdr.grid_propagate(False)

        HexLogo(hdr, size=44).place(x=0, rely=0.5, anchor="w")

        ctk.CTkLabel(hdr, text="ONYX",
                     font=ctk.CTkFont("Courier New", 34, "bold"),
                     text_color=TEXT).place(x=58, y=8, anchor="nw")

        ctk.CTkLabel(hdr, text="local · offline",
                     font=ctk.CTkFont("Courier New", 11),
                     text_color=TEXT3).place(x=60, y=50, anchor="nw")

        self._status_lbl = ctk.CTkLabel(hdr, text="",
                                         font=ctk.CTkFont("Courier New", 11),
                                         text_color=GREEN)
        self._status_lbl.place(relx=1.0, rely=0.5, anchor="e")

    # ── Barre raccourcis ──────────────────────────────────────────────
    def _build_shortcut_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=28, pady=(12, 0))

        # Bouton Raccourcis
        self._shortcuts_btn = ctk.CTkButton(
            bar, text="⚡ Raccourcis",
            height=30, corner_radius=6,
            border_width=1, border_color=BORDER,
            fg_color="transparent", hover_color=SURFACE,
            text_color=BLUE,
            font=ctk.CTkFont("Courier New", 12, "bold"),
            command=self._toggle_shortcuts,
        )
        self._shortcuts_btn.pack(side="left")

        # ── Bouton Aide moi — toujours visible ──────────────────────
        ctk.CTkButton(
            bar, text="👁 Aide moi",
            height=30, corner_radius=6,
            border_width=1, border_color="#1a3a3a",
            fg_color="#0a2020", hover_color="#0f2e2e",
            text_color=CYAN,
            font=ctk.CTkFont("Courier New", 12, "bold"),
            command=self._aide_moi,
        ).pack(side="left", padx=(10, 0))

        # Boutons droite
        ctk.CTkButton(
            bar, text="📱 Téléphone",
            height=30, corner_radius=6,
            border_width=1, border_color=BORDER,
            fg_color="transparent", hover_color=SURFACE,
            text_color=GREEN,
            font=ctk.CTkFont("Courier New", 12),
            command=self._open_qr,
        ).pack(side="right", padx=(0, 8))

        self._voice_btn = ctk.CTkButton(
            bar, text="🎙 Vocal",
            height=30, corner_radius=6,
            border_width=1, border_color=BORDER,
            fg_color="transparent", hover_color=SURFACE,
            text_color=TEXT3,
            font=ctk.CTkFont("Courier New", 12),
            command=self._toggle_vocal,
        )
        self._voice_btn.pack(side="right", padx=(0, 8))

    # ── Aide moi direct ───────────────────────────────────────────────
    def _aide_moi(self) -> None:
        """Envoie 'aide moi' directement sans passer par l'input."""
        if self._thinking:
            return
        self._log("toi", "aide moi", "user")
        self._set_thinking(True)
        threading.Thread(target=self._process, args=("aide moi",), daemon=True).start()

    # ── Panel raccourcis ──────────────────────────────────────────────
    def _toggle_shortcuts(self) -> None:
        if self._shortcuts_visible:
            self._close_shortcuts()
        else:
            self._open_shortcuts()

    def _open_shortcuts(self) -> None:
        self._shortcuts_visible = True
        self._shortcuts_btn.configure(text="✕ Fermer", text_color=TEXT2)

        # Cadre scrollable : 9 catégories / 42 boutons ne tiennent pas d'un coup
        self._shortcuts_panel = ctk.CTkScrollableFrame(
            self, fg_color=BG2,
            border_color=BORDER, border_width=1, corner_radius=8,
            height=300,
        )
        self._shortcuts_panel.grid(row=2, column=0, sticky="ew", padx=28, pady=(8, 0))

        categories: dict[str, list] = {}
        for label, prefill, color, cat in self._ALL_SHORTCUTS:
            categories.setdefault(cat, []).append((label, prefill, color))

        # 3 colonnes indépendantes — chaque catégorie va dans la moins chargée.
        # Vision est traitée en 1er → reste en haut à gauche.
        n_cols = 3
        col_frames = []
        col_load = [0] * n_cols
        for c in range(n_cols):
            self._shortcuts_panel.grid_columnconfigure(c, weight=1, uniform="sc")
            f = ctk.CTkFrame(self._shortcuts_panel, fg_color="transparent")
            f.grid(row=0, column=c, padx=10, pady=(6, 12), sticky="new")
            col_frames.append(f)

        for cat, items in categories.items():
            c = col_load.index(min(col_load))
            col_load[c] += len(items) + 1
            parent = col_frames[c]

            cat_color = CYAN if cat == "Vision" else TEXT3
            ctk.CTkLabel(parent, text=cat.upper(),
                         font=ctk.CTkFont("Courier New", 10),
                         text_color=cat_color).pack(anchor="w", pady=(8, 5))

            for label, prefill, color in items:
                ctk.CTkButton(
                    parent, text=label,
                    height=28, corner_radius=5,
                    border_width=1, border_color=BORDER,
                    fg_color="transparent", hover_color=SURFACE,
                    text_color=color, anchor="w",
                    font=ctk.CTkFont("Courier New", 12),
                    command=lambda p=prefill: self._prefill_and_close(p),
                ).pack(fill="x", pady=2)

    def _close_shortcuts(self) -> None:
        self._shortcuts_visible = False
        self._shortcuts_btn.configure(text="⚡ Raccourcis", text_color=BLUE)
        if self._shortcuts_panel:
            self._shortcuts_panel.destroy()
            self._shortcuts_panel = None

    def _prefill_and_close(self, prefix: str) -> None:
        self._close_shortcuts()
        self._prefill(prefix)

    # ── Chat ──────────────────────────────────────────────────────────
    def _build_chat(self) -> None:
        self._chat = ctk.CTkTextbox(
            self,
            fg_color=BG2,
            border_color=BORDER, border_width=1,
            corner_radius=8,
            font=ctk.CTkFont("Courier New", 13),
            text_color=TEXT,
            wrap="word",
            state="disabled",
        )
        self._chat.grid(row=3, column=0, sticky="nsew", padx=28, pady=(12, 0))

        self._chat.tag_config("who_user",   foreground=TEXT3)
        self._chat.tag_config("who_onyx",   foreground=BLUE)
        self._chat.tag_config("ts",         foreground=TEXT3)
        self._chat.tag_config("msg_user",   foreground=TEXT2)
        self._chat.tag_config("msg_onyx",   foreground=TEXT)
        self._chat.tag_config("msg_action", foreground=GREEN)
        self._chat.tag_config("msg_vision", foreground=CYAN)

        self._log("onyx", "Bonjour — comment puis-je t'aider ?", "onyx")

    # ── Input ─────────────────────────────────────────────────────────
    def _build_input(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=28, pady=(10, 22))
        bar.grid_columnconfigure(0, weight=1)

        self._entry = ctk.CTkEntry(
            bar,
            placeholder_text="→  tape ta demande...",
            height=44,
            corner_radius=8,
            border_color=BORDER,
            fg_color=SURFACE,
            text_color=TEXT,
            placeholder_text_color=TEXT3,
            font=ctk.CTkFont("Courier New", 13),
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._entry.bind("<Return>", lambda _: self._send())

        self._send_btn = ctk.CTkButton(
            bar, text="Envoyer",
            width=100, height=44,
            corner_radius=8,
            fg_color=SURFACE,
            hover_color="#1e1e1e",
            border_width=1, border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont("Courier New", 13, "bold"),
            command=self._send,
        )
        self._send_btn.grid(row=0, column=1)

    # ── Logique ───────────────────────────────────────────────────────
    def _prefill(self, prefix: str) -> None:
        self._entry.delete(0, "end")
        self._entry.insert(0, prefix)
        self._entry.focus()

    def _open_qr(self) -> None:
        if not QR_OK:
            self._log("onyx", "Lance : pip install qrcode pillow", "action")
            return
        if self._qr_popup and self._qr_popup.winfo_exists():
            self._qr_popup.focus()
            return
        self._qr_popup = QRPopup(self, self._server_proc)
        self._qr_popup.focus()

    def _toggle_vocal(self) -> None:
        if not VOICE_OK:
            self._log("onyx",
                "Mode vocal indisponible.\n"
                "Lance : pip install sounddevice numpy openai-whisper pyttsx3", "action")
            return
        if self._voice_active:
            self._stop_vocal()
        else:
            self._start_vocal()

    def _start_vocal(self) -> None:
        self._voice_active = True
        self._voice_btn.configure(text="⏹ Stop vocal", text_color=RED)

        # Ouvre l'overlay animé
        if OVERLAY_OK:
            self._vocal_overlay = VocalOverlay(self, on_close=self._stop_vocal)
            self._vocal_overlay.focus()

        self._voice_mode = VoiceMode(
            on_transcript=self._on_voice_transcript,
            on_result=self._on_voice_result,
            on_status=self._on_voice_status,
            router_fn=router,
            chat_fn=lambda t: extraire_latence(chat_llm(t))[0],  # TTS ne lit pas "[[lat]]"
            on_state_change=self._on_voice_state,
        )
        # start() est non-bloquant : libs/modèle chargés dans le thread vocal.
        # Si échec (deps manquantes…), l'état passe à OFF → _apply_voice_state nettoie.
        self._voice_mode.start()

    def _stop_vocal(self) -> None:
        self._voice_active = False
        if self._voice_mode:
            self._voice_mode.stop()
            self._voice_mode = None
        if self._vocal_overlay:
            try:
                self._vocal_overlay.destroy()
            except Exception:
                pass
            self._vocal_overlay = None
        self._voice_btn.configure(text="🎙 Vocal", text_color=TEXT3)
        self._status_lbl.configure(text="", text_color=GREEN)

    def _on_voice_state(self, state: str) -> None:
        """Appelé depuis voice_mode thread → met à jour l'overlay (via main thread)."""
        self.after(0, lambda s=state: self._apply_voice_state(s))

    def _apply_voice_state(self, state: str) -> None:
        if state == "OFF" and self._voice_active:
            # Le thread vocal s'est arrêté (deps manquantes, erreur mic…)
            self._stop_vocal()
            return
        if self._vocal_overlay:
            try:
                self._vocal_overlay.set_state(state)
            except Exception:
                pass

    def _on_voice_transcript(self, text: str) -> None:
        self.after(0, lambda: self._log("toi", f"🎙 {text}", "user"))

    def _on_voice_result(self, transcript: str, result: str, routed: bool) -> None:
        # `routed` vient de voice_mode — NE PAS rappeler router() ici :
        # ça ré-exécutait l'action (shutdown, message, etc.) une 2e fois.
        is_vision = transcript.lower().startswith("aide moi")
        role      = "vision" if is_vision else ("action" if routed else "onyx")
        self.after(0, lambda r=result, ro=role: self._log("onyx", r, ro))

    def _on_voice_status(self, status: str) -> None:
        if "❌" in status:
            color = RED
        elif any(k in status for k in ("actif", "parle", "écoute")):
            color = GREEN
        else:
            color = TEXT3
        self.after(0, lambda s=status, c=color: self._status_lbl.configure(
            text=s, text_color=c,
        ))

    def _log(self, who: str, text: str, role: str) -> None:
        ts      = datetime.now().strftime("%H:%M")
        who_tag = "who_onyx" if role in ("onyx", "action", "vision") else "who_user"
        msg_tag = "msg_vision" if role == "vision" else \
                  ("msg_action" if role == "action" else
                  ("msg_onyx" if role == "onyx" else "msg_user"))
        who_lbl = "ONYX" if role in ("onyx", "action", "vision") else "TOI"

        self._chat.configure(state="normal")
        self._chat.insert("end", "\n")
        self._chat.insert("end", f" {who_lbl}  ", who_tag)
        self._chat.insert("end", f"{ts}\n", "ts")
        self._chat.insert("end", f" {text}\n", msg_tag)
        self._chat.configure(state="disabled")
        self._chat.see("end")

    def _set_thinking(self, v: bool) -> None:
        self._thinking = v
        self._send_btn.configure(state="disabled" if v else "normal")
        self._status_lbl.configure(
            text="⟳ processing..." if v else "",
            text_color=TEXT3 if v else GREEN,
        )

    def _send(self) -> None:
        txt = self._entry.get().strip()
        if not txt or self._thinking:
            return
        self._entry.delete(0, "end")
        self._log("toi", txt, "user")
        self._set_thinking(True)
        threading.Thread(target=self._process, args=(txt,), daemon=True).start()

    def _process(self, txt: str) -> None:
        try:
            result = router(txt)
            is_vision = txt.lower().strip().startswith("aide moi") or \
                        any(kw in txt.lower() for kw in ["lis mon écran", "regarde mon écran"])
            if result is not None:
                role = "vision" if is_vision else "action"
                self.after(0, lambda r=result, ro=role: self._log("onyx", r, ro))
            else:
                reply, lat = extraire_latence(chat_llm(txt))
                self.after(0, lambda r=reply: self._log("onyx", r, "onyx"))
                if lat is not None:
                    self.after(0, lambda l=lat: self._show_latency(l))
        except Exception as exc:
            self.after(0, lambda: self._log("onyx", f"Erreur interne : {exc}", "action"))
        finally:
            self.after(0, lambda: self._set_thinking(False))

    def _show_latency(self, lat: float) -> None:
        """Affiche la latence LLM dans le header (vert <3s, orange <8s, rouge sinon)."""
        col = GREEN if lat < 3 else (ORANGE if lat < 8 else RED)
        self._status_lbl.configure(text=f"⏱ {lat:.1f}s", text_color=col)


if __name__ == "__main__":
    ONYXApp().mainloop()
