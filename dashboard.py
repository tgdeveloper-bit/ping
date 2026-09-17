"""
Pinger Dashboard — separate router module.
server.py শুধু `app.include_router(dashboard_router)` করবে।
"""
import hashlib
import hmac
import os
import time
import uuid

from fastapi import APIRouter, Cookie, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

# ---------- config (env থেকে লোড) ----------
ADMIN_TOKEN        = os.getenv("ADMIN_TOKEN", "")
DASHBOARD_PASSWORD = (os.getenv("DASHBOARD_PASSWORD") or ADMIN_TOKEN).strip()
SESSION_SECRET     = (
    os.getenv("SESSION_SECRET")
    or hashlib.sha256((ADMIN_TOKEN + "|" + DASHBOARD_PASSWORD + "|pinger-v1").encode()).hexdigest()
)
SESSION_TTL = 7 * 24 * 3600
COOKIE_NAME = "pinger_session"

# ---------- shared state থেকে আসবে (server.py সেট করবে) ----------
# আমরা ফাংশন ইনজেক্ট করব যাতে server.py-এর globals-এ access পাই
_get_targets = lambda: {}
_get_paused  = lambda: False
_set_paused  = lambda v: None
_get_lock    = None
_bulk_add_fn = None


def init(targets_getter, paused_getter, paused_setter, lock, bulk_add_fn):
    """server.py থেকে কল হবে — shared state inject করে।"""
    global _get_targets, _get_paused, _set_paused, _get_lock, _bulk_add_fn
    _get_targets = targets_getter
    _get_paused  = paused_getter
    _set_paused  = paused_setter
    _get_lock    = lock
    _bulk_add_fn = bulk_add_fn


# ---------- session helpers ----------
def _make_session() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _check_session(v: str | None) -> bool:
    if not v or "." not in v:
        return False
    ts, sig = v.rsplit(".", 1)
    try:
        if int(ts) + SESSION_TTL < time.time():
            return False
    except ValueError:
        return False
    expected = hmac.new(SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _require_session(session: str | None) -> None:
    if not _check_session(session):
        raise HTTPException(401, "login required")


# ---------- HTML ----------
LOGIN_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — Pinger</title>
<style>
  :root{--bg:#0b0f17;--fg:#e5e7eb;--muted:#6b7280;--err:#ef4444}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);font-family:ui-monospace,Menlo,monospace;padding:20px}
  .card{width:100%;max-width:360px;background:#111827;border:1px solid #1f2937;
        border-radius:12px;padding:28px 24px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
  h1{margin:0 0 4px;font-size:18px}
  p.sub{margin:0 0 20px;color:var(--muted);font-size:12px}
  label{display:block;font-size:11px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.05em}
  input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #374151;
        background:#0b0f17;color:var(--fg);font-family:inherit;font-size:14px;margin-bottom:14px}
  input:focus{outline:none;border-color:#2563eb}
  button{width:100%;padding:11px;border-radius:8px;border:none;background:#2563eb;color:#fff;
         font-family:inherit;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#1d4ed8}
  .err{background:rgba(239,68,68,.1);border:1px solid var(--err);color:var(--err);
       padding:8px 10px;border-radius:6px;font-size:12px;margin-bottom:14px}
</style></head>
<body>
<form class="card" method="POST" action="/login">
  <h1>🛰️ Pinger</h1>
  <p class="sub">Admin access required</p>
  __ERR__
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">🔓 Unlock Dashboard</button>
</form>
</body></html>
"""


DASHBOARD_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pinger Dashboard</title>
<style>
  :root{--bg:#0b0f17;--ok:#22c55e;--err:#ef4444;--warn:#f59e0b;--muted:#6b7280;--fg:#e5e7eb}
  *{box-sizing:border-box}
  body{margin:0;font-family:ui-monospace,Menlo,monospace;background:var(--bg);color:var(--fg);padding:14px;font-size:13px}
  header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  h1{font-size:16px;margin:0}
  .badge{padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}
  .badge.ok{background:rgba(34,197,94,.15);color:var(--ok);border:1px solid var(--ok)}
  .badge.err{background:rgba(239,68,68,.15);color:var(--err);border:1px solid var(--err)}
  .badge.pause{background:rgba(245,158,11,.15);color:var(--warn);border:1px solid var(--warn)}
  button{font-family:inherit;font-size:12px;padding:5px 9px;border-radius:6px;border:1px solid #374151;
         background:#1f2937;color:var(--fg);cursor:pointer}
  button:hover{background:#374151}
  button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
  button.warn{background:#b45309;border-color:#b45309;color:#fff}
  button.ghost{background:transparent;border-color:#374151;color:var(--muted)}
  .meta{color:var(--muted);font-size:11px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1f2937}
  th{color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.05em}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .dot.ok{background:var(--ok);box-shadow:0 0 6px var(--ok)}
  .dot.err{background:var(--err);box-shadow:0 0 6px var(--err)}
  .dot.pend{background:var(--muted)}
  .url{color:#93c5fd;word-break:break-all}
  .row-err td{background:rgba(239,68,68,.06)}
  .stat{display:inline-block;margin-right:14px}
  .stat b{color:#fff}
  .pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);animation:p 1s infinite}
  @keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
  #addbox{display:none;margin:10px 0;padding:10px;border:1px solid #374151;border-radius:8px;background:#0f172a}
  #addbox textarea{width:100%;min-height:80px;padding:8px;border-radius:6px;border:1px solid #374151;
                   background:#0b0f17;color:var(--fg);font-family:inherit;font-size:12px;resize:vertical}
</style></head>
<body>

<header>
  <h1>🛰️ Pinger</h1>
  <span class="pulse"></span>
  <span class="meta" id="clock">—</span>
  <div style="margin-left:auto;display:flex;gap:6px">
    <button class="warn"    onclick="toggleAll(true)">⏸ Pause</button>
    <button class="primary" onclick="toggleAll(false)">▶ Resume</button>
    <button class="primary" onclick="toggleAdd()">＋ Add</button>
    <button class="ghost"   onclick="logout()">⏻ Logout</button>
  </div>
</header>

<div id="addbox">
  <div class="meta" style="margin-bottom:6px">URLs — এক লাইনে একটা</div>
  <textarea id="addUrls" placeholder="https://example.com/health&#10;https://httpbin.org/get"></textarea>
  <div style="margin-top:8px;display:flex;gap:6px">
    <button class="primary" onclick="doAdd()">Add</button>
    <button class="ghost"   onclick="toggleAdd()">Cancel</button>
  </div>
</div>

<div style="margin-bottom:12px">
  <span class="stat">Status: <b id="state">—</b></span>
  <span class="stat">Total: <b id="total">—</b></span>
  <span class="stat" style="color:var(--ok)">OK: <b id="okc">—</b></span>
  <span class="stat" style="color:var(--err)">Fail: <b id="errc">—</b></span>
</div>

<table>
  <thead><tr><th>St</th><th>Code</th><th>Last Hit</th><th>URL</th><th></th></tr></thead>
  <tbody id="tb"></tbody>
</table>

<script>
async function fetchData(){
  try{
    const r = await fetch("/api/targets", {cache:"no-store", credentials:"same-origin"});
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) { document.getElementById("state").textContent = "REMOTE " + r.status; return; }
    render(await r.json());
  }catch(e){ document.getElementById("state").textContent = "OFFLINE"; }
}
function render(d){
  document.getElementById("state").innerHTML = d.global_paused
    ? '<span class="badge pause">⏸ PAUSED</span>'
    : '<span class="badge ok">▶ RUNNING</span>';
  document.getElementById("total").textContent = d.total;
  const tb = document.getElementById("tb");
  let ok=0, err=0;
  const rows=[];
  for (const [id,t] of Object.entries(d.targets)){
    const s = t.last_status;
    let cls="pend", code="…";
    if (s===200){cls="ok";ok++;code="200";}
    else if (s!==null && s!==undefined){cls="err";err++;code=String(s);}
    rows.push(`
      <tr class="${cls==='err'?'row-err':''}">
        <td><span class="dot ${cls}"></span>${cls==='ok'?'OK':cls==='err'?'FAIL':'—'}</td>
        <td>${code}</td>
        <td class="meta">${(t.last_hit||'—').slice(11,19)}</td>
        <td class="url">${t.url.replace(/^https?:\/\//,'').replace(/\/health$/,'')}</td>
        <td><button onclick="toggleOne('${id}', ${!t.active})">${t.active?'⏸':'▶'}</button>
            <button onclick="delOne('${id}')" style="color:#ef4444">✕</button></td>
      </tr>`);
  }
  tb.innerHTML = rows.join("");
  document.getElementById("okc").textContent = ok;
  document.getElementById("errc").textContent = err;
}
async function toggleAll(pause){
  await fetch(pause?"/api/pause-all":"/api/resume-all",{method:"POST",credentials:"same-origin"});
  fetchData();
}
async function toggleOne(id, active){
  await fetch(`/api/targets/${id}`,{method:"PATCH",credentials:"same-origin",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({active})});
  fetchData();
}
async function delOne(id){
  if (!confirm("Delete this target?")) return;
  await fetch(`/api/targets/${id}`,{method:"DELETE",credentials:"same-origin"});
  fetchData();
}
function toggleAdd(){
  const b=document.getElementById("addbox");
  b.style.display = b.style.display==="block" ? "none":"block";
  if (b.style.display==="block") document.getElementById("addUrls").focus();
}
async function doAdd(){
  const txt=document.getElementById("addUrls").value.trim();
  if (!txt) return;
  const urls=txt.split(/\s+/).filter(u=>/^https?:\/\//.test(u));
  if (!urls.length){ alert("No valid http(s) URLs"); return; }
  const r=await fetch("/api/bulk-add",{method:"POST",credentials:"same-origin",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({urls})});
  const j=await r.json();
  alert(`Added ${j.added||0}, total ${j.total||0}`);
  document.getElementById("addUrls").value="";
  toggleAdd();
  fetchData();
}
async function logout(){
  await fetch("/logout",{method:"POST",credentials:"same-origin"});
  location.href="/login";
}
setInterval(()=>{
  document.getElementById("clock").textContent =
    new Date().toISOString().replace("T"," ").slice(11,19)+"Z";
},1000);
fetchData();
setInterval(fetchData, 5000);
</script>
</body></html>
"""


# ================= Router =================

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML.replace("__ERR__", "")


@router.post("/login")
async def do_login(password: str = Form(...)):
    if not DASHBOARD_PASSWORD or not hmac.compare_digest(password.strip(), DASHBOARD_PASSWORD):
        html = LOGIN_HTML.replace("__ERR__", '<div class="err">❌ Wrong password</div>')
        return HTMLResponse(html, status_code=401)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, _make_session(),
        max_age=SESSION_TTL, httponly=True, samesite="strict",
        secure=True, path="/",
    )
    return resp


@router.post("/logout")
async def do_logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(session: str = Cookie(None)):
    if not _check_session(session):
        return RedirectResponse("/login", status_code=303)
    return DASHBOARD_HTML


# ---------- APIs (cookie auth) ----------
@router.get("/api/targets")
async def api_targets(session: str = Cookie(None)):
    _require_session(session)
    t = _get_targets()
    return {
        "global_paused": _get_paused(),
        "total": len(t),
        "limit": 100, "offset": 0,
        "targets": t,
    }


@router.post("/api/pause-all")
async def api_pause_all(session: str = Cookie(None)):
    _require_session(session)
    _set_paused(True)
    return {"global_paused": True}


@router.post("/api/resume-all")
async def api_resume_all(session: str = Cookie(None)):
    _require_session(session)
    _set_paused(False)
    return {"global_paused": False}


@router.post("/api/bulk-add")
async def api_bulk_add(body: dict, session: str = Cookie(None)):
    _require_session(session)
    urls = body.get("urls") or []
    return await _bulk_add_fn(urls)


@router.patch("/api/targets/{tid}")
async def api_patch(tid: str, body: dict, session: str = Cookie(None)):
    _require_session(session)
    t = _get_targets().get(tid)
    if not t:
        raise HTTPException(404, "target not found")
    t["active"] = bool(body.get("active", t["active"]))
    return {"id": tid, "active": t["active"]}


@router.delete("/api/targets/{tid}")
async def api_delete(tid: str, session: str = Cookie(None)):
    _require_session(session)
    async with _get_lock():
        t = _get_targets()
        if tid not in t:
            raise HTTPException(404, "target not found")
        del t[tid]
    return {"deleted": tid}