from __future__ import annotations
import asyncio, logging, os, secrets, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pinger")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "20"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "8"))
USER_AGENT = "UptimePinger/1.0 (+https://example.com/bot)"

# ---------- state ----------
targets: dict[str, dict] = {}
GLOBAL_PAUSED = False
_lock = asyncio.Lock()          # protects `targets` mutations


# ---------- auth ----------
def check_token(token: str | None) -> None:
    # constant-time compare to avoid timing attacks
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "Invalid admin token")


# ---------- validation ----------
def valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# ---------- worker ----------
async def _ping_one(sem: asyncio.Semaphore, client: httpx.AsyncClient, tid: str, t: dict) -> None:
    async with sem:
        if GLOBAL_PAUSED or not t.get("active"):
            return
        try:
            r = await client.get(t["url"], timeout=REQUEST_TIMEOUT)
            t["last_status"] = r.status_code
        except Exception as e:
            t["last_status"] = f"ERR:{type(e).__name__}"
        finally:
            t["last_hit"] = datetime.now(timezone.utc).isoformat()


async def hit_targets() -> None:
    if GLOBAL_PAUSED or client is None:
        return
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    # snapshot to avoid "dict changed size" if admin mutates during run
    snapshot = list(targets.items())
    await asyncio.gather(*(_ping_one(sem, client, tid, t) for tid, t in snapshot))


# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, scheduler
    client = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=MAX_CONCURRENCY * 2),
        follow_redirects=True,
    )
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        hit_targets, "interval", minutes=1, id="main_job",
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    scheduler.start()
    log.info("started; concurrency=%d timeout=%.1fs", MAX_CONCURRENCY, REQUEST_TIMEOUT)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await client.aclose()


app = FastAPI(lifespan=lifespan)

client: httpx.AsyncClient | None = None
scheduler: AsyncIOScheduler | None = None


# ---------- models ----------
class BulkAdd(BaseModel):
    urls: list[HttpUrl]

    @field_validator("urls")
    @classmethod
    def _dedupe(cls, v):
        seen, out = set(), []
        for u in v:
            s = str(u)
            if s not in seen:
                seen.add(s); out.append(u)
        return out


class TargetPatch(BaseModel):
    active: bool


# ---------- routes ----------
@app.get("/health")
async def health():
    return {"ok": True, "global_paused": GLOBAL_PAUSED, "targets": len(targets)}


@app.post("/admin/bulk-add")
async def bulk_add(body: BulkAdd, x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    added = 0
    async with _lock:
        existing = {t["url"] for t in targets.values()}
        for u in body.urls:
            s = str(u)
            if s in existing:
                continue
            tid = uuid.uuid4().hex[:8]        # collision-free
            targets[tid] = {"url": s, "active": True, "last_status": None, "last_hit": None}
            existing.add(s)
            added += 1
    return {"added": added, "total": len(targets)}


@app.get("/admin/targets")
async def list_targets(limit: int = 100, offset: int = 0,
                       x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    items = list(targets.items())[offset: offset + limit]
    return {
        "global_paused": GLOBAL_PAUSED,
        "total": len(targets),
        "limit": limit, "offset": offset,
        "targets": dict(items),
    }


@app.post("/admin/pause-all")
async def pause_all(x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    global GLOBAL_PAUSED
    GLOBAL_PAUSED = True
    return {"global_paused": True}


@app.post("/admin/resume-all")
async def resume_all(x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    global GLOBAL_PAUSED
    GLOBAL_PAUSED = False
    return {"global_paused": False}


@app.post("/admin/targets/{tid}/pause")
async def pause_one(tid: str, x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    t = targets.get(tid) or _404()
    t["active"] = False
    return {"id": tid, "active": False}


@app.post("/admin/targets/{tid}/resume")
async def resume_one(tid: str, x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    t = targets.get(tid) or _404()
    t["active"] = True
    return {"id": tid, "active": True}


@app.patch("/admin/targets/{tid}")
async def patch_one(tid: str, body: TargetPatch, x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    t = targets.get(tid) or _404()
    t["active"] = body.active
    return {"id": tid, "active": body.active}


@app.delete("/admin/targets/{tid}")
async def delete_one(tid: str, x_admin_token: str = Header(None)):
    check_token(x_admin_token)
    async with _lock:
        if tid not in targets:
            _404()
        del targets[tid]
    return {"deleted": tid}


def _404():
    raise HTTPException(404, "target not found")