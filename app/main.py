from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import store
from .engine import CANDLE_TICKS, PAIR_CFG, Engine, TradeError

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"

engine = Engine()
last_order_at: dict[int, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    engine.load()
    task = asyncio.create_task(engine.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="FX 生存战", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def current_user(authorization: str = Header(default="")) -> int:
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    uid = store.user_id_by_token(token)
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    return uid


def optional_user(authorization: str = Header(default="")) -> Optional[int]:
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    return store.user_id_by_token(token) if token else None


class AuthIn(BaseModel):
    username: str = Field(min_length=2, max_length=24)
    password: str = Field(min_length=6, max_length=128)


class OrderIn(BaseModel):
    pair: str
    side: Literal[1, -1]
    amount: float
    leverage: float = 100

class DepositIn(BaseModel):
    amount: float = Field(gt=0)

@app.post("/api/register")
async def register(body: AuthIn):
    try:
        uid = await asyncio.to_thread(store.create_user, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    async with engine.lock:
        engine.ensure_account(uid)
    return {"token": store.issue_token(uid)}


@app.post("/api/login")
async def login(body: AuthIn):
    uid = await asyncio.to_thread(store.auth_user, body.username, body.password)
    if not uid:
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    async with engine.lock:
        engine.ensure_account(uid)
    return {"token": store.issue_token(uid)}


@app.get("/api/state")
async def state(uid: int = Depends(current_user)):
    trades = await asyncio.to_thread(store.recent_trades, uid, 15)
    async with engine.lock:
        engine.ensure_account(uid)
        return {
            "account": engine.account_view(uid),
            "positions": engine.positions_view(uid),
            "prices": engine.prices_view(),
            "headline": engine.headline,
            "news": engine.news_view(),
            "config": engine.config_view(),
            "trades": trades,
        }


@app.post("/api/orders")
async def create_order(body: OrderIn, uid: int = Depends(current_user)):
    now = time.time()
    if now - last_order_at.get(uid, 0) < 0.3:
        raise HTTPException(status_code=429, detail="操作太快了")
    last_order_at[uid] = now

    async with engine.lock:
        engine.ensure_account(uid)
        try:
            pos = engine.open_position(uid, body.pair, body.side, body.amount, body.leverage)
            return {"ok": True, "position_id": pos.id}
        except TradeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/positions/{pid}/close")
async def close_position(pid: int, uid: int = Depends(current_user)):
    async with engine.lock:
        try:
            pnl = engine.close_position(pid, uid=uid)
            return {"ok": True, "pnl": round(pnl)}
        except TradeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/reset")
async def reset(uid: int = Depends(current_user)):
    async with engine.lock:
        try:
            engine.reset_account(uid)
            return {"ok": True}
        except TradeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/deposit")
async def deposit(body: DepositIn, uid: int = Depends(current_user)):
    async with engine.lock:
        try:
            return engine.deposit(uid, body.amount)
        except TradeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/history")
async def history(pair: str = "USDJPY"):
    if pair not in PAIR_CFG:
        raise HTTPException(status_code=400, detail="未知货币对")
    async with engine.lock:
        points = [round(x, 3) for x in engine.history[pair]]
        tick = engine.tick
    return {
        "pair": pair,
        "points": points,
        "tick": tick,
        "candle_ticks": CANDLE_TICKS,
    }

@app.get("/api/trades")
async def trades(limit: int = 10, offset: int = 0,
                 uid: int = Depends(current_user)):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    async with engine.lock:
        return engine.trades_view(uid, limit, offset)

@app.get("/api/users/{username}")
async def user_profile(username: str, viewer: Optional[int] = Depends(optional_user)):
    uid = await asyncio.to_thread(store.user_id_by_name, username)
    if not uid:
        raise HTTPException(status_code=404, detail="用户不存在")
    async with engine.lock:
        data = engine.profile_view(uid, viewer)
    if not data:
        raise HTTPException(status_code=404, detail="用户不存在")
    return data

@app.get("/api/leaderboard")
async def leaderboard(uid: Optional[int] = Depends(optional_user)):
    async with engine.lock:
        rows = engine.leaderboard(uid)
        total = len(engine.accounts)
    my_rank = next((r["rank"] for r in rows if r["me"]), None)
    return {"total": total, "rows": rows[:20], "me": my_rank}


@app.get("/api/health")
async def health():
    return {"ok": True, "tick": engine.tick, "players": len(engine.accounts)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
    except Exception:
        await ws.close(code=4401)
        return

    if not isinstance(hello, dict):
        await ws.close(code=4401)
        return

    uid = await asyncio.to_thread(store.user_id_by_token, hello.get("token", ""))
    if not uid:
        await ws.close(code=4401)
        return

    async with engine.lock:
        engine.ensure_account(uid)
        snapshot = {
            "type": "hello",
            "tick": engine.tick,
            "prices": engine.prices_view(),
            "account": engine.account_view(uid),
            "positions": engine.positions_view(uid),
            "headline": engine.headline,
            "config": engine.config_view(),
        }

    q = engine.subscribe(uid)
    try:
        await ws.send_json(snapshot)
        while True:
            await ws.send_json(await q.get())
    except Exception:
        pass
    finally:
        engine.unsubscribe(q)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")