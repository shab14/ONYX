"""
vocal_overlay.py — ONYX Vocal Overlay v1
Orbe animé inspiré du HUD de MARK XL, adapté en tkinter Canvas.
États : LISTENING / THINKING / SPEAKING / MUTED / OFF

Usage depuis gui.py :
    from vocal_overlay import VocalOverlay
    overlay = VocalOverlay(master)
    overlay.set_state("LISTENING")
    overlay.destroy()
"""
from __future__ import annotations

import math
import random
import tkinter as tk
from typing import Literal

import customtkinter as ctk

# ── Palette ONYX (inspirée MARK XL) ──────────────────────────────────────────
_BG      = "#00060a"
_PRI     = "#00d4ff"   # cyan principal
_PRI_DIM = "#007a99"
_PRI_GHO = "#001f2e"
_ACC     = "#ff6b00"   # orange → SPEAKING
_ACC2    = "#ffcc00"   # jaune  → THINKING
_GREEN   = "#00ff88"   # vert   → LISTENING
_MUTED   = "#ff3366"   # rouge  → MUTED
_TEXT    = "#8ffcff"
_BORDER  = "#0d3347"
_BORDER_B = "#1a5c7a"

State = Literal["LISTENING", "THINKING", "SPEAKING", "MUTED", "WAITING_WAKE", "OFF"]

_STATE_COLOR: dict[str, str] = {
    "LISTENING":    _GREEN,
    "THINKING":     _ACC2,
    "SPEAKING":     _ACC,
    "MUTED":        _MUTED,
    "WAITING_WAKE": _PRI_DIM,
    "OFF":          _PRI_DIM,
}

_STATE_LABEL: dict[str, str] = {
    "LISTENING":    "● LISTENING",
    "THINKING":     "◈ THINKING",
    "SPEAKING":     "● SPEAKING",
    "MUTED":        "⊘ MUTED",
    "WAITING_WAKE": "zzz  HEY ONYX ?",
    "OFF":          "○ OFFLINE",
}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_col: str, alpha: float) -> str:
    """Retourne une couleur tkinter avec alpha simulé sur fond _BG."""
    r, g, b   = _hex_to_rgb(hex_col)
    br, bg, bb = _hex_to_rgb(_BG)
    a = max(0.0, min(1.0, alpha))
    nr = int(br + (r - br) * a)
    ng = int(bg + (g - bg) * a)
    nb = int(bb + (b - bb) * a)
    return f"#{nr:02x}{ng:02x}{nb:02x}"


class VocalOverlay(ctk.CTkToplevel):
    """
    Fenêtre flottante avec orbe animé.
    S'ouvre quand vocal démarre, se ferme quand vocal s'arrête.
    """

    SIZE = 340  # taille de la fenêtre carrée

    def __init__(self, master: tk.Misc, on_close: "callable | None" = None) -> None:
        super().__init__(master)
        self.title("ONYX — Vocal")
        self.geometry(f"{self.SIZE}x{self.SIZE + 60}")
        self.resizable(False, False)
        self.configure(fg_color=_BG)
        self.attributes("-topmost", True)

        self._on_close   = on_close
        self._state: State = "LISTENING"
        self._tick       = 0
        self._blink      = True
        self._blink_cnt  = 0

        # Animation state
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._waveform   = [3] * 36
        # Couleur d'orbe interpolée (transitions douces entre états)
        self._orb_rgb    = [0.0, 55.0, 100.0]
        self._prev_state = "LISTENING"
        self._trans      = 0.0   # 0→1 progression de transition d'état

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._animate()

    def _build(self) -> None:
        S = self.SIZE

        # Canvas principal
        self._canvas = tk.Canvas(
            self, width=S, height=S,
            bg=_BG, highlightthickness=0,
        )
        self._canvas.pack(pady=(0, 0))

        # Bouton fermer / mute en bas
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(6, 10))

        self._mute_btn = ctk.CTkButton(
            bar, text="🎙 Mute",
            height=32, corner_radius=6,
            border_width=1, border_color=_BORDER_B,
            fg_color="transparent", hover_color="#0a1a24",
            text_color=_TEXT,
            font=ctk.CTkFont("Courier New", 12),
            command=self._toggle_mute,
        )
        self._mute_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

        ctk.CTkButton(
            bar, text="✕ Fermer",
            height=32, corner_radius=6,
            border_width=1, border_color=_BORDER,
            fg_color="transparent", hover_color="#0a1a24",
            text_color=_PRI_DIM,
            font=ctk.CTkFont("Courier New", 12),
            command=self._close,
        ).pack(side="left", expand=True, fill="x")

    # ── API publique ──────────────────────────────────────────────────────────

    def set_state(self, state: State) -> None:
        if state != self._state:
            self._prev_state = self._state
            self._trans      = 0.0   # relance l'interpolation
        self._state = state

    def get_state(self) -> State:
        return self._state

    # ── Logique interne ───────────────────────────────────────────────────────

    def _toggle_mute(self) -> None:
        if self._state == "MUTED":
            self.set_state("LISTENING")
            self._mute_btn.configure(text="🎙 Mute")
        else:
            self.set_state("MUTED")
            self._mute_btn.configure(text="🔇 Unmute")

    def _close(self) -> None:
        if self._on_close:
            self._on_close()
        self.destroy()

    # ── Boucle d'animation (16ms ≈ 60fps) ────────────────────────────────────

    def _animate(self) -> None:
        try:
            self._step()
            self._draw()
            self.after(16, self._animate)
        except tk.TclError:
            pass  # fenêtre détruite

    def _step(self) -> None:
        self._tick += 1
        speaking = self._state == "SPEAKING"
        muted    = self._state == "MUTED"
        waiting  = self._state == "WAITING_WAKE"

        # Avance la transition d'état (ease)
        if self._trans < 1.0:
            self._trans = min(1.0, self._trans + 0.06)

        # Cible halo + scale selon état
        if self._tick % (4 if speaking else 20) == 0:
            if speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            elif waiting:
                self._tgt_scale = random.uniform(0.999, 1.004)
                self._tgt_halo  = random.uniform(30, 45)
            else:
                self._tgt_scale = random.uniform(1.001, 1.010)
                self._tgt_halo  = random.uniform(48, 72)

        # WAITING_WAKE : "respiration" lente sinusoïdale
        if waiting:
            breath = 0.5 + 0.5 * math.sin(self._tick * 0.04)
            self._tgt_halo  = 28 + breath * 28
            self._tgt_scale = 0.99 + breath * 0.03

        sp = 0.38 if speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        # Couleur d'orbe cible selon état → interpolée doucement
        if muted:        tgt = (180, 0, 40)
        elif speaking:   tgt = (0, 80, 140)
        elif waiting:    tgt = (40, 30, 90)
        elif self._state == "THINKING": tgt = (120, 90, 0)
        else:            tgt = (0, 55, 100)   # LISTENING
        for i in range(3):
            self._orb_rgb[i] += (tgt[i] - self._orb_rgb[i]) * 0.12

        # Rings
        speeds = [1.3, -0.9, 2.0] if speaking else \
                 ([0.18, -0.12, 0.3] if waiting else [0.55, -0.35, 0.9])
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        # Pulses
        S   = self.SIZE
        lim = S * 0.74
        spd = 4.2 if speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        emit = 0.07 if speaking else (0.012 if waiting else 0.025)
        if len(self._pulses) < 3 and random.random() < emit:
            self._pulses.append(0.0)

        # Waveform
        if speaking:
            self._waveform = [random.randint(3, 22) for _ in range(36)]
        elif self._state == "THINKING":
            self._waveform = [
                int(4 + 3 * math.sin(self._tick * 0.15 + i * 0.5))
                for i in range(36)
            ]
        elif waiting:
            self._waveform = [int(2 + 1.5 * math.sin(self._tick * 0.05 + i * 0.3)) for i in range(36)]
        else:
            self._waveform = [
                int(3 + 2 * math.sin(self._tick * 0.06 + i * 0.6))
                for i in range(36)
            ]

        # Blink
        self._blink_cnt += 1
        if self._blink_cnt >= 30:
            self._blink = not self._blink
            self._blink_cnt = 0

    def _draw(self) -> None:
        c  = self._canvas
        S  = self.SIZE
        cx = cy = S / 2
        c.delete("all")

        state    = self._state
        speaking = state == "SPEAKING"
        muted    = state == "MUTED"
        pri_col  = _MUTED if muted else _PRI

        # Fond
        c.create_rectangle(0, 0, S, S, fill=_BG, outline="")

        # Points de grille
        for x in range(0, S, 48):
            for y in range(0, S, 48):
                c.create_oval(x-1, y-1, x+1, y+1,
                              fill=_rgba(_BORDER_B, 0.3), outline="")

        r_face = S * 0.31

        # Halo glow (cercles concentriques)
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0.0, self._halo * 0.085 * frc / 255)
            col = _rgba(pri_col, a * 2.5)
            c.create_oval(cx-r, cy-r, cx+r, cy+r,
                          outline=col, width=1)

        # Pulse rings
        for pr in self._pulses:
            a   = max(0.0, (1.0 - pr / (S * 0.74)) * 0.7)
            col = _rgba(pri_col, a)
            c.create_oval(cx-pr, cy-pr, cx+pr, cy+pr,
                          outline=col, width=1)

        # Spinning arc rings (approx via arcs)
        for idx, (r_frac, width, arc_len, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = S * r_frac
            base   = self._rings[idx]
            a_val  = max(0.0, self._halo * (1.0 - idx * 0.18) / 255)
            col    = _rgba(pri_col, min(1.0, a_val * 3))
            angle  = base
            x0, y0 = cx - ring_r, cy - ring_r
            x1, y1 = cx + ring_r, cy + ring_r
            while angle < base + 360:
                start = angle % 360
                c.create_arc(x0, y0, x1, y1,
                             start=start, extent=arc_len,
                             outline=col, width=width, style="arc")
                angle += arc_len + gap

        # Tick marks
        t_out, t_in = S * 0.497, S * 0.474
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 5
            x1 = cx + t_out * math.cos(rad)
            y1 = cy - t_out * math.sin(rad)
            x2 = cx + inn  * math.cos(rad)
            y2 = cy - inn  * math.sin(rad)
            c.create_line(x1, y1, x2, y2,
                          fill=_rgba(_PRI, 0.45), width=1)

        # Crosshair
        ch_r, gap_h = S * 0.51, S * 0.16
        ch_col = _rgba(pri_col, self._halo * 0.5 / 255)
        c.create_line(cx - ch_r, cy, cx - gap_h, cy, fill=ch_col)
        c.create_line(cx + gap_h, cy, cx + ch_r, cy, fill=ch_col)
        c.create_line(cx, cy - ch_r, cx, cy - gap_h, fill=ch_col)
        c.create_line(cx, cy + gap_h, cx, cy + ch_r, fill=ch_col)

        # Corner brackets
        bl = 22
        hl, hr = cx - S // 2 + 2, cx + S // 2 - 2
        ht, hb = cy - S // 2 + 2, cy + S // 2 - 2
        bc = _rgba(_PRI, 0.75)
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            c.create_line(bx, by, bx + dx*bl, by, fill=bc, width=2)
            c.create_line(bx, by, bx, by + dy*bl, fill=bc, width=2)

        # Orbe central — couleur interpolée (transitions douces entre états)
        orb_r = int(S * 0.27 * self._scale)
        oc = (int(self._orb_rgb[0]), int(self._orb_rgb[1]), int(self._orb_rgb[2]))

        for i in range(8, 0, -1):
            r2  = int(orb_r * i / 8)
            frc = i / 8
            a   = max(0, min(255, int(self._halo * 1.1 * frc)))
            col = f"#{int(oc[0]*frc):02x}{int(oc[1]*frc):02x}{int(oc[2]*frc):02x}"
            # tkinter ne supporte pas l'alpha natif → on blende avec _BG
            blended = _rgba(col, a / 255)
            c.create_oval(cx-r2, cy-r2, cx+r2, cy+r2,
                          fill=blended, outline="")

        # Texte ONYX au centre
        c.create_text(cx, cy, text="ONYX",
                      font=("Courier New", 13, "bold"),
                      fill=_rgba(_TEXT, min(1.0, self._halo * 2 / 255)))

        # Texte état
        state_col = _STATE_COLOR.get(state, _PRI)
        if state == "THINKING":
            lbl = f"{'◈' if self._blink else '◇'}  THINKING"
        elif state == "LISTENING":
            lbl = f"{'●' if self._blink else '○'}  LISTENING"
        elif state == "WAITING_WAKE":
            lbl = f"{'zzz' if self._blink else 'z..'}  HEY ONYX ?"
        else:
            lbl = _STATE_LABEL.get(state, state)

        sy = cy + S * 0.40
        c.create_text(cx, sy, text=lbl,
                      font=("Courier New", 11, "bold"),
                      fill=state_col)

        # Waveform
        wy = sy + 22
        N, bw = 36, 7
        wx0 = cx - (N * bw) / 2
        for i, hgt in enumerate(self._waveform):
            if muted:
                col = _rgba(_MUTED, 0.5)
                hgt = 2
            elif speaking:
                col = _rgba(_PRI, 0.9) if hgt > 12 else _rgba(_PRI_DIM, 0.6)
            else:
                col = _rgba(_BORDER_B, 0.8)
            x = wx0 + i * bw
            c.create_rectangle(x, wy + 20 - hgt, x + bw - 1, wy + 20,
                                fill=col, outline="")
