import asyncio, secrets, string, os, logging, time
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Header, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, inspect, text
from sqlalchemy.orm import Session
from .db import Base, engine, SessionLocal, get_db
from .models import Room, Franchise, Player, Lot, Bid, Sale, Event
from .security import new_token, token_hash
from .config import AUCTION_SOURCE, DEFAULT_PURSE, DEFAULT_BASE_PRICE, MAX_SQUAD, MIN_SQUAD
from .seed import load_players, load_players_csv
from .engine import (get_room, get_franchise_by_token, create_queue, open_next_lot, seconds_until,
                     place_bid, settle_lot, pause_room, resume_room, undo_last_sale,
                     undo_last_bid, redo_last_bid, redo_last_sale, nominated_unsold, snapshot, event, now_utc, recover_interrupted_rooms, lock_for)

Base.metadata.create_all(bind=engine)
# Lightweight additive migration for rooms created before redo support.
with engine.begin() as conn:
    inspector = inspect(conn)
    for table in ("bids", "sales"):
        existing = {column["name"] for column in inspector.get_columns(table)}
        if "redo_available" not in existing:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN redo_available BOOLEAN NOT NULL DEFAULT FALSE")
    lot_columns = {column["name"] for column in inspector.get_columns("lots")}
    if "settled_at" not in lot_columns:
        conn.exec_driver_sql("ALTER TABLE lots ADD COLUMN settled_at DATETIME NULL")
    if "settlement_redo_available" not in lot_columns:
        conn.exec_driver_sql("ALTER TABLE lots ADD COLUMN settlement_redo_available BOOLEAN NOT NULL DEFAULT FALSE")
    room_columns = {column["name"] for column in inspector.get_columns("rooms")}
    if "host_franchise_id" not in room_columns:
        conn.exec_driver_sql("ALTER TABLE rooms ADD COLUMN host_franchise_id INTEGER NULL")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smat-auction")

app = FastAPI(title="SMAT Private Auction", version="1.0.0")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

class CreateRoom(BaseModel):
    name: str = "SMAT Auction"
    franchises: list[str] = Field(min_length=2, max_length=10)
    purse: int = Field(default=DEFAULT_PURSE, ge=100)
    timer_seconds: int = Field(default=10)
    min_squad: int = Field(default=MIN_SQUAD, ge=1)
    max_squad: int = Field(default=MAX_SQUAD, ge=1)
    bid_extension: bool = True
    players: list[dict] | None = None

class BidRequest(BaseModel):
    amount: int

class PlayerEdit(BaseModel):
    name: str | None = None
    state_team: str | None = None
    role: str | None = None
    base_price: int | None = Field(default=None, ge=1)
    importance_weight: float | None = Field(default=None, gt=0)
    included: bool | None = None

class NominationRequest(BaseModel):
    player_ids: list[int]
    price_overrides: dict[str, int] = {}

class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}
    async def connect(self, code, ws):
        await ws.accept()
        self.connections.setdefault(code, set()).add(ws)
    def disconnect(self, code, ws):
        self.connections.get(code, set()).discard(ws)
    async def broadcast(self, code, message):
        # Send concurrently: one slow phone must not hold up every other bidder.
        sockets = list(self.connections.get(code, set()))
        if not sockets:
            return
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=2.0)
                return None
            except Exception:
                return ws
        results = await asyncio.gather(*(send(ws) for ws in sockets))
        for ws in results:
            if ws is not None:
                self.disconnect(code, ws)

manager = ConnectionManager()
timer_tasks: dict[str, asyncio.Task] = {}

def fail(exc):
    raise HTTPException(status_code=400, detail=str(exc))

def auth_context(db: Session, code: str, authorization: str | None):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing invite token")
    token = authorization.split(" ", 1)[1].strip()
    room = get_room(db, code)
    hashed = token_hash(token)
    if secrets.compare_digest(room.host_token_hash, hashed):
        return room, None, True
    franchise = get_franchise_by_token(db, room.id, hashed)
    if not franchise:
        raise HTTPException(401, "Invalid invite token")
    return room, franchise, False

def require_host(db, code, authorization):
    room, franchise, is_host = auth_context(db, code, authorization)
    if not is_host:
        raise HTTPException(403, "Host permission required")
    return room

def _snapshot_for_room(code):
    with SessionLocal() as db:
        room = get_room(db, code)
        return snapshot(db, room)

async def broadcast_snapshot(code):
    try:
        data = await asyncio.to_thread(_snapshot_for_room, code)
    except Exception:
        return
    await manager.broadcast(code, {"type": "snapshot", "data": data})

def _read_timer_deadline(code):
    with SessionLocal() as db:
        room = get_room(db, code)
        if room.status != "LIVE" or not room.current_lot_id:
            return None
        lot = db.get(Lot, room.current_lot_id)
        if not lot or lot.status != "LIVE" or not lot.ends_at:
            return None
        return lot.ends_at

def _settle_expired_lot(code):
    with SessionLocal() as db:
        room = get_room(db, code)
        # Serialize expiry settlement with incoming bids for this room.
        with lock_for(room.id):
            db.refresh(room)
            if room.status != "LIVE" or not room.current_lot_id:
                return False, False
            lot = db.get(Lot, room.current_lot_id)
            if not lot or lot.status != "LIVE" or not lot.ends_at or seconds_until(lot.ends_at) > 0:
                return False, room.status == "LIVE"
            settle_lot(db, room)
            open_next_lot(db, room)
            db.commit()
            return True, room.status == "LIVE"

async def timer_supervisor(code):
    """Sleep to the persisted deadline; never poll PostgreSQL several times per second."""
    try:
        deadline = await asyncio.to_thread(_read_timer_deadline, code)
        if deadline is None:
            return
        delay = max(0.0, seconds_until(deadline))
        await asyncio.sleep(delay)
        settled, still_live = await asyncio.to_thread(_settle_expired_lot, code)
        if settled:
            await broadcast_snapshot(code)
        if still_live:
            arm_timer(code)
    except asyncio.CancelledError:
        return
    except Exception:
        # Keep the durable state untouched if the database is temporarily unavailable.
        # The host can resume/retry; no in-memory-only settlement is attempted.
        return

def arm_timer(code):
    task = timer_tasks.get(code)
    if task and not task.done():
        task.cancel()
    try:
        timer_tasks[code] = asyncio.create_task(timer_supervisor(code))
    except RuntimeError:
        pass

@app.on_event("startup")
async def recover_live_rooms():
    # Do not restart a live timer automatically. Recover the durable auction
    # state, pause the room, and let the host explicitly resume with a full timer.
    with SessionLocal() as db:
        recover_interrupted_rooms(db)

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store, max-age=0"})

@app.get("/health")
def health(db: Session = Depends(get_db)):
    # Render uses this endpoint to determine whether the service is healthy.
    db.execute(text("SELECT 1"))
    return {"ok": True, "database": "ok", "service": "smat-auction"}

@app.post("/api/rooms")
def create_room(payload: CreateRoom, db: Session = Depends(get_db)):
    if payload.timer_seconds not in (5, 10, 25):
        raise HTTPException(400, "Timer must be 5, 10 or 25 seconds")
    if payload.max_squad < payload.min_squad:
        raise HTTPException(400, "Maximum squad must be at least the minimum squad")
    names = [x.strip() for x in payload.franchises if x.strip()]
    if len(names) != len(payload.franchises) or len(set(n.lower() for n in names)) != len(names):
        raise HTTPException(400, "Franchise names must be non-empty and unique")
    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    host_token = new_token()
    room = Room(code=code, name=payload.name, host_token_hash=token_hash(host_token),
                purse=payload.purse, timer_seconds=payload.timer_seconds,
                min_squad=payload.min_squad, max_squad=payload.max_squad,
                bid_extension=payload.bid_extension)
    db.add(room)
    db.flush()
    invite_links = []
    for i, name in enumerate(names):
        token = new_token()
        f = Franchise(room_id=room.id, name=name, invite_hash=token_hash(token),
                      purse_remaining=payload.purse, created_order=i)
        db.add(f)
        db.flush()
        invite_links.append({"franchise": name, "token": token,
                             "url": f"/?room={code}&token={token}"})
    players = payload.players
    if players is None:
        source = Path(AUCTION_SOURCE)
        if source.exists():
            players = load_players(str(source), DEFAULT_BASE_PRICE)
        else:
            players = []
    for i, p in enumerate(players):
        db.add(Player(room_id=room.id, name=p["name"], state_team=p.get("state_team", ""),
                      role=p.get("role", "UNKNOWN"), base_price=int(p.get("base_price", DEFAULT_BASE_PRICE)),
                      importance_weight=float(p.get("importance_weight", 1.0)),
                      included=bool(p.get("included", True)), source_order=int(p.get("source_order", i))))
    event(db, room.id, "ROOM_CREATED", "Auction room created.", {"franchises": names})
    db.commit()
    return {"room_code": code, "host_token": host_token,
            "host_url": f"/?room={code}&token={host_token}",
            "franchise_invites": invite_links, "players_loaded": len(players)}

@app.get("/api/rooms/{code}/me")
def get_me(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room, franchise, is_host = auth_context(db, code, authorization)
    if is_host and room.host_franchise_id:
        franchise = db.get(Franchise, room.host_franchise_id)
    return {"is_host": is_host, "franchise": ({"id": franchise.id, "name": franchise.name} if franchise else None)}

@app.get("/api/rooms/{code}/snapshot")
def get_snapshot(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room, _, _ = auth_context(db, code, authorization)
    return snapshot(db, room)

@app.get("/api/rooms/{code}/pool")
def get_pool(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room, _, _ = auth_context(db, code, authorization)
    rows = db.scalars(select(Player).where(Player.room_id == room.id).order_by(Player.state_team, Player.name)).all()
    return [{"id": p.id, "name": p.name, "state_team": p.state_team, "role": p.role,
             "base_price": p.base_price, "importance_weight": p.importance_weight,
             "included": p.included} for p in rows]

@app.patch("/api/rooms/{code}/players/{player_id}")
def edit_player(code: str, player_id: int, payload: PlayerEdit,
                authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "Player pool can only be edited before the auction starts")
    p = db.scalar(select(Player).where(Player.id == player_id, Player.room_id == room.id))
    if not p:
        raise HTTPException(404, "Player not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, key, value)
    db.commit()
    return {"ok": True}

@app.post("/api/rooms/{code}/players")
def add_player(code: str, payload: dict, authorization: str | None = Header(default=None),
               db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "Players can only be added before the auction starts")
    if not payload.get("name"):
        raise HTTPException(400, "Player name is required")
    p = Player(room_id=room.id, name=payload["name"].strip(),
               state_team=payload.get("state_team", ""), role=payload.get("role", "UNKNOWN").upper(),
               base_price=int(payload.get("base_price", DEFAULT_BASE_PRICE)),
               importance_weight=float(payload.get("importance_weight", 1.0)), included=True)
    db.add(p); db.commit()
    return {"id": p.id, "ok": True}

@app.post("/api/rooms/{code}/players/import-csv")
async def import_csv(code: str, file: UploadFile = File(...),
                     authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "CSV import is only available before the auction starts")
    content = await file.read()
    temp = Path("/tmp/smat_players_import.csv")
    temp.write_bytes(content)
    try:
        players = load_players_csv(str(temp))
        for p in players:
            db.add(Player(room_id=room.id, **p, included=True))
        db.commit()
    except Exception as e:
        db.rollback()
        fail(e)
    return {"imported": len(players)}

@app.post("/api/rooms/{code}/start")
async def start_auction(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "Auction has already started")
    players = db.scalars(select(Player).where(Player.room_id == room.id, Player.included == True)).all()
    if not players:
        raise HTTPException(400, "The auction pool is empty")
    if len(players) < room.min_squad * len(db.scalars(select(Franchise).where(Franchise.room_id == room.id)).all()):
        # Warn only; auction can still run if the host intentionally wants fewer purchases.
        event(db, room.id, "POOL_WARNING", "Player pool is smaller than all minimum squad slots combined.")
    create_queue(db, room, players)
    db.flush()
    open_next_lot(db, room)
    room.version += 1
    db.commit()
    arm_timer(code)
    result = snapshot(db, room)
    await broadcast_snapshot(code)
    return result

def _process_bid(code: str, authorization: str | None, amount: int):
    started = time.perf_counter()
    with SessionLocal() as db:
        room, franchise, is_host = auth_context(db, code, authorization)
        if is_host:
            franchise = db.get(Franchise, room.host_franchise_id) if room.host_franchise_id else None
            if not franchise:
                raise HTTPException(403, "Select your franchise in Host controls before bidding.")
        auth_done = time.perf_counter()
        try:
            place_bid(db, room, franchise, amount)
            place_done = time.perf_counter()
            db.commit()  # A bid is acknowledged only after its durable commit.
            commit_done = time.perf_counter()
            result = snapshot(db, room)
            snapshot_done = time.perf_counter()
            logger.info(
                "bid_timing room=%s auth_ms=%.1f validation_write_ms=%.1f commit_ms=%.1f snapshot_ms=%.1f total_ms=%.1f",
                code, (auth_done-started)*1000, (place_done-auth_done)*1000,
                (commit_done-place_done)*1000, (snapshot_done-commit_done)*1000,
                (snapshot_done-started)*1000
            )
            return result
        except ValueError as e:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/rooms/{code}/bid")
async def bid(code: str, payload: BidRequest, authorization: str | None = Header(default=None)):
    # SQLAlchemy uses a synchronous psycopg connection. Run the whole transaction
    # off the event loop so it cannot stall WebSockets, timers, or other bidders.
    result = await asyncio.to_thread(_process_bid, code, authorization, payload.amount)
    arm_timer(code)
    asyncio.create_task(manager.broadcast(code, {"type": "snapshot", "data": result}))
    return result

@app.post("/api/rooms/{code}/host/select-franchise")
async def select_host_franchise(code: str, payload: dict,
        authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "Choose the host's franchise before the auction starts")
    franchise_id = payload.get("franchise_id")
    franchise = db.scalar(select(Franchise).where(Franchise.id == franchise_id, Franchise.room_id == room.id)) if franchise_id else None
    if franchise_id and not franchise:
        raise HTTPException(404, "Franchise not found in this room")
    room.host_franchise_id = franchise.id if franchise else None
    room.version += 1
    db.commit()
    result = snapshot(db, room)
    await broadcast_snapshot(code)
    return result

def _process_host_action(code: str, action: str, authorization: str | None):
    with SessionLocal() as db:
        room = require_host(db, code, authorization)
        try:
            if action == "pause":
                pause_room(db, room)
            elif action == "resume":
                resume_room(db, room)
            elif action == "advance":
                if room.status == "LIVE":
                    lot = db.get(Lot, room.current_lot_id)
                    if lot:
                        lot.ends_at = now_utc()
                        settle_lot(db, room)
                open_next_lot(db, room)
            elif action == "undo-sale":
                undo_last_sale(db, room)
                if room.status not in ("LIVE", "PAUSED", "COMPLETED"):
                    open_next_lot(db, room)
            elif action == "undo-bid":
                undo_last_bid(db, room)
            elif action == "redo-bid":
                redo_last_bid(db, room)
            elif action == "redo-sale":
                redo_last_sale(db, room)
                if room.current_lot_id is None and room.status not in ("LIVE", "PAUSED", "COMPLETED"):
                    open_next_lot(db, room)
            elif action == "finish":
                if room.current_lot_id:
                    active_lot = db.get(Lot, room.current_lot_id)
                    if active_lot and active_lot.status in ("LIVE", "PAUSED"):
                        active_lot.status = "LIVE"
                        active_lot.ends_at = now_utc()
                        settle_lot(db, room)
                room.status = "COMPLETED"
                room.current_lot_id = None
                event(db, room.id, "COMPLETED", "Auction marked complete.")
            else:
                raise ValueError("Unknown host action")
            room.version += 1
            db.commit()
            return snapshot(db, room)
        except ValueError as e:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/rooms/{code}/host/{action}")
async def host_action(code: str, action: str, authorization: str | None = Header(default=None)):
    result = await asyncio.to_thread(_process_host_action, code, action, authorization)
    if action in ("resume", "advance", "undo-bid", "redo-bid", "undo-sale", "redo-sale"):
        arm_timer(code)
    elif action in ("pause", "finish"):
        task = timer_tasks.get(code)
        if task:
            task.cancel()
    asyncio.create_task(manager.broadcast(code, {"type": "snapshot", "data": result}))
    return result

@app.post("/api/rooms/{code}/nominate-unsold")
async def nominate(code: str, payload: NominationRequest,
             authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    try:
        nominated_unsold(db, room, payload.player_ids, payload.price_overrides)
        db.commit()
    except ValueError as e:
        db.rollback()
        fail(e)
    if room.status == "LIVE":
        arm_timer(code)
    result = snapshot(db, room)
    await broadcast_snapshot(code)
    return result

@app.websocket("/ws/{code}")
async def websocket_endpoint(ws: WebSocket, code: str, token: str):
    with SessionLocal() as db:
        try:
            room = get_room(db, code)
            hashed = token_hash(token)
            valid = secrets.compare_digest(room.host_token_hash, hashed) or bool(
                get_franchise_by_token(db, room.id, hashed)
            )
            if not valid:
                await ws.close(code=1008)
                return
        except Exception:
            await ws.close(code=1008)
            return
    await manager.connect(code, ws)
    await broadcast_snapshot(code)
    try:
        while True:
            # Browser clients send an application-level heartbeat so hosted
            # proxies can detect and preserve otherwise-idle WebSocket sessions.
            # Auction mutations still only use authenticated HTTP endpoints.
            message = await ws.receive_text()
            if message == "ping" or message == '{"type":"ping"}':
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(code, ws)

@app.get("/api/rooms/{code}/events")
def get_events(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room, _, _ = auth_context(db, code, authorization)
    rows = db.scalars(select(Event).where(Event.room_id == room.id).order_by(Event.id.desc()).limit(100)).all()
    return [{"kind": e.kind, "message": e.message, "created_at": e.created_at.isoformat()} for e in rows]

@app.post("/api/rooms/{code}/reset")
def reset_room(code: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    room = require_host(db, code, authorization)
    if room.status != "SETUP":
        raise HTTPException(400, "Reset is only allowed before starting")
    db.commit()
    return {"ok": True, "message": "Room is already in setup state"}

