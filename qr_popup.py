"""
qr_popup.py — ONYX QR Code popup v2
Fix : check si serveur déjà actif avant de relancer.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import tkinter as tk

import customtkinter as ctk

from config import SERVER_PORT

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


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def generer_qr(url: str, size: int = 200):
    import qrcode
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="white", back_color="#0c0c0c")
    return img.resize((size, size))


class QRPopup(ctk.CTkToplevel):
    def __init__(self, master, server_process_ref: list) -> None:
        super().__init__(master)
        self.title("ONYX — Connexion mobile")
        self.geometry("320x440")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self._server_proc = server_process_ref
        self._build()

    def _build(self) -> None:
        ip   = get_local_ip()
        url  = f"http://{ip}:{SERVER_PORT}"

        ctk.CTkLabel(
            self, text="📱 Connexion mobile",
            font=ctk.CTkFont("Courier New", 15, "bold"),
            text_color=TEXT,
        ).pack(pady=(20, 4))

        ctk.CTkLabel(
            self, text="Scanne le QR code avec ton téléphone",
            font=ctk.CTkFont("Courier New", 11),
            text_color=TEXT2,
        ).pack(pady=(0, 16))

        try:
            from PIL import ImageTk
            pil_img = generer_qr(url, size=200)
            self._tk_img = ImageTk.PhotoImage(pil_img)
            lbl = tk.Label(self, image=self._tk_img, bg=BG, bd=0)
            lbl.pack()
        except ImportError:
            ctk.CTkLabel(
                self, text="pip install qrcode pillow",
                font=ctk.CTkFont("Courier New", 11),
                text_color=ORANGE,
            ).pack()

        ctk.CTkLabel(
            self, text=url,
            font=ctk.CTkFont("Courier New", 13, "bold"),
            text_color=BLUE,
        ).pack(pady=(14, 4))

        ctk.CTkLabel(
            self, text="même réseau WiFi requis",
            font=ctk.CTkFont("Courier New", 10),
            text_color=TEXT3,
        ).pack()

        ctk.CTkLabel(
            self, text="🔒 PIN habituel requis",
            font=ctk.CTkFont("Courier New", 10),
            text_color=TEXT2,
        ).pack(pady=(8, 0))

        self._status = ctk.CTkLabel(
            self, text="⟳ Démarrage du serveur...",
            font=ctk.CTkFont("Courier New", 11),
            text_color=ORANGE,
        )
        self._status.pack(pady=(12, 0))

        ctk.CTkButton(
            self, text="Fermer et arrêter le serveur",
            height=34, corner_radius=6,
            border_width=1, border_color=BORDER,
            fg_color="transparent", hover_color=SURFACE,
            text_color=TEXT2,
            font=ctk.CTkFont("Courier New", 11),
            command=self._fermer,
        ).pack(pady=(14, 20))

        threading.Thread(target=self._start_server, daemon=True).start()

    def _start_server(self) -> None:
        if _port_in_use(SERVER_PORT):
            self.after(0, lambda: self._status.configure(
                text=f"✓ Serveur déjà actif (port {SERVER_PORT})",
                text_color=GREEN,
            ))
            return

        if self._server_proc:
            self.after(0, lambda: self._status.configure(
                text="✓ Serveur déjà en cours",
                text_color=GREEN,
            ))
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, "server.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            self._server_proc.append(proc)
            self.after(2000, lambda: self._status.configure(
                text="✓ Serveur actif — prêt à scanner",
                text_color=GREEN,
            ))
        except Exception as e:
            self.after(0, lambda: self._status.configure(
                text=f"Erreur : {e}",
                text_color=ORANGE,
            ))

    def _fermer(self) -> None:
        for proc in self._server_proc:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        self._server_proc.clear()
        self.destroy()
