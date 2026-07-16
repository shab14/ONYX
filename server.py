"""
server.py — ONYX WiFi Server v3
FIXES:
- Auth par session token (POST /auth) — plus de PIN à chaque requête
- CORS restreint aux origines LAN (pas de wildcard *)
- Rate limit sur /auth uniquement, pas reset à chaque succès
- Endpoint /logout pour révoquer session
- Input size guard sur /chat
- Meilleure gestion d'erreur JSON
"""
from __future__ import annotations

import html
import logging
import socket

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, field_validator

from auth import server_rate_limiter, session_store, verify_pin
from config import SERVER_HOST, SERVER_PORT
from main import chat_llm, extraire_latence, router

try:
    from shortcuts import by_category
    _SHORTCUTS_OK = True
except ImportError:
    _SHORTCUTS_OK = False
    def by_category(): return {}

log = logging.getLogger(__name__)

app = FastAPI(title="ONYX API", docs_url=None, redoc_url=None)  # désactive swagger en prod

# CORS restreint : LAN seulement. Autorise null pour les webviews mobiles locales.
_LAN_ORIGINS = [
    "http://localhost",
    f"http://localhost:{SERVER_PORT}",
    "http://127.0.0.1",
    f"http://127.0.0.1:{SERVER_PORT}",
    "null",  # fichiers locaux / webview Android
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_LAN_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-ONYX-Session"],
    allow_credentials=False,
)

_MAX_INPUT_LEN = 4000  # caractères max par message


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "unknown"


def _require_session(request: Request) -> None:
    """Vérifie le session token. Lève 401/429 selon le cas."""
    ip = _client_ip(request)
    if server_rate_limiter.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Trop de tentatives — réessaie dans 5 min.")
    token = request.headers.get("X-ONYX-Session", "")
    if not token or not session_store.validate(token):
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")


# ── Auth ──────────────────────────────────────────────────────────────────────
class AuthIn(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def pin_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("PIN vide")
        if len(v) > 64:
            raise ValueError("PIN trop long")
        return v


class AuthOut(BaseModel):
    session_token: str
    expires_in: int  # secondes


@app.post("/auth", response_model=AuthOut)
async def auth(body: AuthIn, request: Request):
    """Échange PIN → session token. Rate-limité par IP."""
    ip = _client_ip(request)
    if server_rate_limiter.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Trop de tentatives — réessaie dans 5 min.")

    if not verify_pin(body.pin):
        server_rate_limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="PIN invalide.")

    # Auth OK — NE PAS reset le rate limiter (évite attaque par reset)
    token = session_store.create()
    return AuthOut(session_token=token, expires_in=3600)


@app.post("/logout")
async def logout(request: Request):
    token = request.headers.get("X-ONYX-Session", "")
    if token:
        session_store.revoke(token)
    return {"status": "ok"}


# ── Chat ──────────────────────────────────────────────────────────────────────
class MessageIn(BaseModel):
    texte: str

    @field_validator("texte")
    @classmethod
    def texte_not_too_long(cls, v: str) -> str:
        if len(v) > _MAX_INPUT_LEN:
            raise ValueError(f"Message trop long (max {_MAX_INPUT_LEN} chars)")
        return v


class MessageOut(BaseModel):
    reponse: str
    type: str


@app.post("/chat", response_model=MessageOut)
def chat(msg: MessageIn, request: Request):
    # `def` (PAS async) : router/chat_llm sont bloquants (LLM 10-120s).
    # En async, ils gelaient l'event loop uvicorn → serveur entier figé.
    # En def, FastAPI exécute dans un threadpool → requêtes parallèles OK.
    _require_session(request)
    txt = msg.texte.strip()
    if not txt:
        return MessageOut(reponse="Message vide.", type="action")
    try:
        result = router(txt)
        if result is not None:
            return MessageOut(reponse=result, type="action")
        reply, lat = extraire_latence(chat_llm(txt))
        suffixe = f"\n\n⏱ {lat:.1f}s" if lat is not None else ""
        return MessageOut(reponse=reply + suffixe, type="llm")
    except Exception as exc:
        log.exception("Erreur /chat")
        raise HTTPException(status_code=500, detail="Erreur interne.") from exc


@app.get("/ping")
async def ping(request: Request):
    _require_session(request)
    return {"status": "ok", "name": "ONYX"}


# ── UI Mobile ─────────────────────────────────────────────────────────────────
MOBILE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ONYX</title>
<style>
  :root{
    --bg:#0c0c0c;--sf:#181818;--sf2:#1c1c1c;--bd:#2a2a2a;
    --tx:#e8e8e8;--t2:#888;--t3:#444;
    --gr:#4ade80;--bl:#60a5fa;--or:#fb923c;--rd:#f87171;--pu:#a78bfa;
    --cy:#22d3ee;--cy-bg:#0a2020;--cy-bd:#1a3a3a;
  }
  *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
  html,body{height:100%;height:100dvh;background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;overflow:hidden}
  #app{display:flex;flex-direction:column;height:100%;height:100dvh;position:relative}
  #pin-screen{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;z-index:200}
  #pin-screen h2{font-size:22px;letter-spacing:4px;color:var(--tx)}
  #pin-screen p{font-size:11px;color:var(--t2)}
  #pin-entry{background:var(--sf);border:1px solid var(--bd);color:var(--tx);font-family:'Courier New',monospace;font-size:24px;text-align:center;padding:12px 20px;border-radius:8px;width:200px;letter-spacing:8px;outline:none}
  #pin-err{font-size:11px;color:var(--rd);min-height:16px}
  #pin-btn{background:var(--sf);border:1px solid var(--bd);color:var(--tx);font-family:'Courier New',monospace;padding:11px 32px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:bold}
  header{display:flex;align-items:center;padding:14px 18px 12px;border-bottom:1px solid var(--bd);flex-shrink:0;gap:10px}
  h1{font-size:20px;font-weight:bold;letter-spacing:3px}
  .sub{font-size:9px;color:var(--t3);margin-top:2px}
  #sta{margin-left:auto;font-size:11px;color:var(--t2)}
  #sta.on{color:var(--or)}
  #aide-bar{flex-shrink:0;padding:8px 14px;border-bottom:1px solid var(--bd);background:var(--cy-bg)}
  #aide-btn{width:100%;padding:12px;border-radius:8px;background:transparent;border:1px solid var(--cy-bd);color:var(--cy);font-family:'Courier New',monospace;font-size:14px;font-weight:bold;cursor:pointer;letter-spacing:1px;transition:background .15s}
  #aide-btn:active{background:#0f2e2e}
  #shortcuts-bar{flex-shrink:0;border-bottom:1px solid var(--bd);background:var(--sf)}
  #sc-toggle{display:flex;align-items:center;justify-content:space-between;padding:11px 16px;cursor:pointer;user-select:none;font-size:12px;color:var(--bl);letter-spacing:1px;font-weight:bold}
  #sc-toggle:active{background:var(--sf2)}
  #sc-arrow{transition:transform .2s;display:inline-block;color:var(--t2)}
  #sc-arrow.open{transform:rotate(180deg)}
  #sc-list{max-height:0;overflow:hidden;transition:max-height .3s ease}
  #sc-list.open{max-height:55vh;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
  #sc-inner{padding:6px 14px 16px;display:flex;flex-direction:column;gap:12px}
  .sc-cat{font-size:9px;color:var(--t3);letter-spacing:2px;padding:8px 2px 0;font-weight:bold}
  .sc-cat.vision{color:var(--cy)}
  .sc-row{display:flex;flex-wrap:wrap;gap:8px}
  .sc-btn{background:transparent;border:1px solid var(--bd);font-family:'Courier New',monospace;font-size:12px;padding:9px 12px;border-radius:5px;cursor:pointer;white-space:nowrap;flex-shrink:0;min-height:36px;transition-property:background-color,border-color;transition-duration:.15s}
  .sc-btn:active{background:var(--sf);border-color:var(--t2)}
  .sc-btn.vision{border-color:var(--cy-bd);background:var(--cy-bg)}
  .sc-btn.vision:active{background:#0f2e2e}
  .c-blue{color:var(--bl)}.c-green{color:var(--gr)}.c-orange{color:var(--or)}
  .c-purple{color:var(--pu)}.c-red{color:var(--rd)}.c-grey{color:var(--t2)}.c-cyan{color:var(--cy)}
  #chat{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;padding:14px 16px;display:flex;flex-direction:column;gap:14px}
  .msg{display:flex;flex-direction:column;gap:3px}
  .mw{font-size:9px;font-weight:bold;letter-spacing:1.5px;margin-bottom:2px}
  .mw.on{color:var(--bl)}.mw.me{color:var(--t3)}
  .mt{font-size:13px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
  .mt.ac{color:var(--gr)}.mt.on{color:var(--tx)}.mt.me{color:var(--t2)}.mt.vc{color:var(--cy)}
  .ib{flex-shrink:0;padding:10px 14px calc(10px + env(safe-area-inset-bottom));border-top:1px solid var(--bd);display:flex;gap:8px;background:var(--bg)}
  #inp{flex:1;background:var(--sf);border:1px solid var(--bd);border-radius:8px;color:var(--tx);font-family:'Courier New',monospace;font-size:14px;padding:11px 14px;outline:none}
  #inp::placeholder{color:var(--t3)}
  #inp:focus{border-color:var(--t2)}
  #sbtn{background:var(--sf);border:1px solid var(--bd);border-radius:8px;color:var(--tx);font-family:'Courier New',monospace;font-size:13px;font-weight:bold;padding:11px 16px;cursor:pointer;flex-shrink:0;transition:opacity .15s}
  #sbtn:disabled{opacity:.35}
</style>
</head>
<body>
<div id="pin-screen">
  <h2>ONYX</h2>
  <p>Entrez votre code PIN</p>
  <input id="pin-entry" type="password" inputmode="numeric" placeholder="••••" maxlength="20"/>
  <div id="pin-err"></div>
  <button id="pin-btn">Déverrouiller</button>
</div>
<div id="app">
  <header>
    <div><h1>ONYX</h1><div class="sub">local · wifi</div></div>
    <div id="sta"></div>
  </header>
  <div id="aide-bar">
    <button id="aide-btn">👁 Aide moi</button>
  </div>
  <div id="shortcuts-bar">
    <div id="sc-toggle"><span>⚡ RACCOURCIS</span><span id="sc-arrow">▼</span></div>
    <div id="sc-list">
      <div id="sc-inner">{SHORTCUTS_HTML}</div>
          </div>
    </div>
  </div>
  <div id="chat"></div>
  <div class="ib">
    <input id="inp" type="text" placeholder="→ tape ta demande..." autocomplete="off" enterkeyhint="send" maxlength="4000"/>
    <button id="sbtn">Envoyer</button>
  </div>
</div>
<script>
// Session token post-auth (remplace PIN-à-chaque-requête)
let SESSION='';let thinking=false;let scOpen=false;

async function checkPin(){
  const t=document.getElementById('pin-entry').value.trim();
  if(!t)return;
  document.getElementById('pin-btn').disabled=true;
  let locked=false;
  try{
    const r=await fetch('/auth',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({pin:t})
    });
    if(r.ok){
      const d=await r.json();
      SESSION=d.session_token;
      document.getElementById('pin-screen').style.display='none';
      addMsg('onyx','Connecté ✓','on');
      // Efface le PIN de la mémoire DOM
      document.getElementById('pin-entry').value='';
    } else if(r.status===429){
      document.getElementById('pin-err').textContent='Trop de tentatives — attends 5 min.';
      document.getElementById('pin-entry').disabled=true;
      locked=true;
    } else {
      document.getElementById('pin-err').textContent='Code incorrect.';
    }
  } catch(e){
    document.getElementById('pin-err').textContent='Erreur réseau.';
  }
  if(!locked) document.getElementById('pin-btn').disabled=false;
}

document.getElementById('pin-btn').addEventListener('click',checkPin);
document.getElementById('pin-entry').addEventListener('keydown',e=>{if(e.key==='Enter')checkPin();});
document.getElementById('sc-toggle').addEventListener('click',()=>{
  scOpen=!scOpen;
  document.getElementById('sc-list').classList.toggle('open',scOpen);
  document.getElementById('sc-arrow').classList.toggle('open',scOpen);
});
document.getElementById('aide-btn').addEventListener('click',()=>{if(!thinking&&SESSION)send('aide moi');});
document.querySelectorAll('.sc-btn[data-fill]').forEach(btn=>{
  btn.addEventListener('click',()=>fill(btn.dataset.fill));
});
function fill(prefix){
  if(scOpen){scOpen=false;document.getElementById('sc-list').classList.remove('open');document.getElementById('sc-arrow').classList.remove('open');}
  const i=document.getElementById('inp');i.value=prefix;i.focus();i.setSelectionRange(prefix.length,prefix.length);
}
function addMsg(who,text,role){
  const chat=document.getElementById('chat');
  const d=document.createElement('div');d.className='msg';
  const isOnyx=role==='on'||role==='ac'||role==='vc';
  d.innerHTML='<div class="mw '+(isOnyx?'on':'me')+'">'+(isOnyx?'ONYX':'TOI')+'</div>'+
    '<div class="mt '+role+'">'+text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>')+'</div>';
  chat.appendChild(d);
  requestAnimationFrame(()=>{chat.scrollTop=chat.scrollHeight;});
}
function setThinking(v){
  thinking=v;
  document.getElementById('sbtn').disabled=v;
  document.getElementById('aide-btn').disabled=v;
  document.getElementById('aide-btn').style.opacity=v?'0.4':'1';
  const sta=document.getElementById('sta');sta.textContent=v?'⟳ processing...':'';sta.className=v?'on':'';
}
async function send(texte){
  if(!texte.trim()||thinking||!SESSION)return;
  const isVision=texte.toLowerCase().startsWith('aide moi')||texte.toLowerCase().includes('lis mon écran');
  addMsg('me',texte,'me');setThinking(true);
  try{
    const r=await fetch('/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-ONYX-Session':SESSION},
      body:JSON.stringify({texte})
    });
    if(r.status===401){
      // Session expirée → redemander PIN
      SESSION='';
      document.getElementById('pin-screen').style.display='flex';
      addMsg('onyx','Session expirée — reconnecte-toi.','ac');
      setThinking(false);return;
    }
    if(r.status===429){addMsg('onyx','Trop de tentatives — attends 5 min.','ac');setThinking(false);return;}
    const d=await r.json();
    const role=isVision?'vc':(d.type==='action'?'ac':'on');
    addMsg('onyx',d.reponse,role);
  }catch(e){addMsg('onyx','Erreur réseau : '+e,'ac');}
  setThinking(false);
}
function sendInput(){
  const i=document.getElementById('inp');const t=i.value.trim();
  if(!t)return;i.value='';send(t);
}
document.getElementById('sbtn').addEventListener('click',sendInput);
document.getElementById('inp').addEventListener('keydown',e=>{if(e.key==='Enter')sendInput();});
</script>
</body>
</html>"""


def _build_shortcuts_html() -> str:
    """Génère les raccourcis mobiles depuis shortcuts.py (même source que le PC)."""
    rows: list[str] = []
    for cat, items in by_category().items():
        is_vision = cat == "Vision"
        cat_cls = ' vision' if is_vision else ''
        rows.append(f'<div class="sc-cat{cat_cls}">{html.escape(cat.upper())}</div>')
        rows.append('<div class="sc-row">')
        for label, fill, color in items:
            # "aide moi" simple = déjà le gros bouton du haut → on évite le doublon
            if fill.strip() == "aide moi":
                continue
            btn_cls = f'sc-btn c-{color}' + (' vision' if is_vision else '')
            rows.append(
                f'<button class="{btn_cls}" '
                f'data-fill="{html.escape(fill, quote=True)}">'
                f'{html.escape(label)}</button>'
            )
        rows.append('</div>')
    return "\n        ".join(rows)


# Page mobile finale (raccourcis injectés une fois au démarrage)
_MOBILE_PAGE = MOBILE_HTML.replace("{SHORTCUTS_HTML}", _build_shortcuts_html())


@app.get("/", response_class=HTMLResponse)
def mobile_ui():
    return HTMLResponse(content=_MOBILE_PAGE)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = get_local_ip()
    print("=" * 50)
    print("  ONYX — Serveur WiFi")
    print(f"  PC  : http://localhost:{SERVER_PORT}")
    print(f"  Tel : http://{ip}:{SERVER_PORT}")
    print("  PIN : utilise ton code habituel")
    print("=" * 50)
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
