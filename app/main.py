import asyncio, json, re, secrets, string, os, logging, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Header, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, inspect, text, and_
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError, OperationalError
from . import perf
from .db import Base, engine, SessionLocal, get_db
from .models import Room, Franchise, Player, Lot, Bid, Sale, Event
from .security import new_token, new_password, normalize_credential, token_hash
from .config import AUCTION_SOURCE, DEFAULT_PURSE, DEFAULT_BASE_PRICE, MAX_SQUAD, MIN_SQUAD
from .seed import load_players, load_players_csv
from .engine import (result_pause_for, get_room, get_franchise_by_token, create_queue, open_next_lot, seconds_until,
                     place_bid, settle_lot, pause_room, resume_room, undo_last_sale,
                     undo_last_bid, redo_last_bid, redo_last_sale, nominated_unsold, snapshot, event, now_utc,
                     recover_interrupted_rooms, lock_for,
                     BidRejected, AuthError, load_bid_context, validate_bid, apply_bid, compact_state, iso_utc)

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
class RevalidatingStatic(StaticFiles):
    """Static files are always revalidated (ETag/304) so a redeploy can never leave browsers on an old
    app.js that does not understand the server's messages."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

app.mount("/static", RevalidatingStatic(directory=STATIC), name="static")

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
    # EXACT-AMOUNT BIDDING: `amount` is the price the bidder saw and chose. The server
    # never raises it; if it is not exactly the price required right now the bid is
    # rejected with 409 STALE_BID. `lot_id`/`player_id` pin the bid to the player the
    # bidder was looking at (a bid for player A can never land on player B).
    # `franchise_id` is optional and only ever CHECKED against the franchise that the
    # invite token controls; the token, not this field, decides who is bidding.
    amount: int = Field(gt=0)
    lot_id: int | None = None
    player_id: int | None = None
    franchise_id: int | None = None

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

SEND_TIMEOUT = 5.0      # seconds a single socket write may take before that client is dropped
SEND_QUEUE_MAX = 256    # per-client backlog; a client this far behind is dropped and recovers via snapshot


class FanoutTracker:
    """Measures commit -> socket-write time for one broadcast (server-side, excludes network)."""
    def __init__(self, code, version, t_commit, recipients):
        self.code, self.version, self.t_commit, self.n = code, version, t_commit, recipients
        self.done = 0; self.ok = 0; self.lat = []

    def record(self, ok):
        self.done += 1
        self.ok += 1 if ok else 0
        self.lat.append((time.perf_counter() - self.t_commit) * 1000)
        if self.done >= self.n:
            logger.info("bid_broadcast room=%s version=%s recipients=%d ok=%d failed=%d max_ms=%.1f avg_ms=%.1f",
                        self.code, self.version, self.n, self.ok, self.n - self.ok,
                        max(self.lat), sum(self.lat) / len(self.lat))


class Connection:
    """One WebSocket with its own outbound queue + writer task, so a slow or dead
    client can only ever delay itself, and messages to it keep their commit order."""
    def __init__(self, ws):
        self.ws = ws
        self.queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.task = None
        self.closed = False

    def send(self, text, tracker=None):
        if self.closed:
            return False
        try:
            self.queue.put_nowait((text, tracker))
            return True
        except asyncio.QueueFull:
            return False


class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[Connection]] = {}

    async def connect(self, code, ws):
        await ws.accept()
        conn = Connection(ws)
        conn.task = asyncio.create_task(self._writer(code, conn))
        self.connections.setdefault(code.upper(), set()).add(conn)
        return conn

    def disconnect(self, code, conn):
        self._forget(code, conn)
        if conn.task and not conn.task.done():
            conn.task.cancel()

    def _forget(self, code, conn):
        conn.closed = True
        self.connections.get(code.upper(), set()).discard(conn)

    async def _writer(self, code, conn):
        close_socket = False
        try:
            while True:
                text, tracker = await conn.queue.get()
                try:
                    await asyncio.wait_for(conn.ws.send_text(text), timeout=SEND_TIMEOUT)
                    ok = True
                except Exception:
                    ok = False
                if tracker:
                    tracker.record(ok)
                if not ok:
                    close_socket = True
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._forget(code, conn)
            while not conn.queue.empty():           # anything still queued was not delivered
                _, tracker = conn.queue.get_nowait()
                if tracker:
                    tracker.record(False)
        if close_socket:
            try:
                await conn.ws.close(code=1011)       # the client reconnects and recovers by snapshot
            except Exception:
                pass

    def broadcast_nowait(self, code, message, t_commit=None):
        """Queue `message` for every client in the room. Never blocks on any client.
        Must run on the event-loop thread (publish() takes care of that)."""
        conns = list(self.connections.get(code.upper(), ()))
        if not conns:
            return
        text = json.dumps(message, separators=(",", ":"))   # serialise once, not per client
        tracker = None
        if message.get("type") == "BID_ACCEPTED" and t_commit is not None:
            tracker = FanoutTracker(code, message.get("room_version"), t_commit, len(conns))
        for conn in conns:
            if not conn.send(text, tracker):
                if tracker:
                    tracker.record(False)
                self.disconnect(code, conn)
                asyncio.ensure_future(self._close_quietly(conn.ws))

    async def _close_quietly(self, ws):
        try:
            await ws.close(code=1013)
        except Exception:
            pass

    async def broadcast(self, code, message):
        self.broadcast_nowait(code, message)

manager = ConnectionManager()
timer_tasks: dict[str, asyncio.Task] = {}
_loop: asyncio.AbstractEventLoop | None = None

def _ensure_loop():
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

def publish(code, message):
    """Thread-safe: hand `message` to the event loop for fan-out to the room's WebSockets.

    Called from the worker thread that just committed, while it still holds the room lock,
    so messages are queued in exactly the order the database committed them. This call only
    schedules work; it cannot block, and a failure here can never undo a committed change."""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(manager.broadcast_nowait, code, message, time.perf_counter())
    except Exception:
        logger.warning("publish failed room=%s", code)

def fail(exc):
    raise HTTPException(status_code=400, detail=str(exc))

# (room code, token hash) pairs whose credential has been verified against the database.
# Used ONLY to decide whether a bid request may take the room lock without a separate
# pre-check query. Every bid still re-verifies its credential inside the single
# authenticate-and-read query (engine.load_bid_context), so this never grants access.
_verified: set[tuple[str, str]] = set()

def _remember(code, hashed):
    if len(_verified) > 2048:
        _verified.clear()
    _verified.add((code.upper(), hashed))

def _bearer_hash(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing invite token")
    return token_hash(authorization.split(" ", 1)[1].strip())

def auth_context(db: Session, code: str, authorization: str | None):
    """Room + (franchise, is_host) for the bearer token, in ONE query (was two)."""
    hashed = _bearer_hash(authorization)
    row = db.execute(
        select(Room, Franchise)
        .outerjoin(Franchise, and_(Franchise.room_id == Room.id, Franchise.invite_hash == hashed))
        .where(Room.code == code.upper()).limit(1)
    ).first()
    if row is None:
        raise HTTPException(404, "Room not found")
    room, franchise = row
    if secrets.compare_digest(room.host_token_hash, hashed):
        _remember(code, hashed)
        return room, None, True
    if franchise is None:
        raise HTTPException(401, "Invalid invite token")
    _remember(code, hashed)
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
    """Settle the current player if (and only if) its deadline has really passed.

    Holds the same room lock as bids from the state read to the commit, so a bid that is
    mid-commit can never be settled underneath, and a settled player can never accept a bid.
    Returns (settled, next_deadline); next_deadline is the (new) live deadline, or None."""
    with lock_for(code):
        with SessionLocal() as db:
            room = get_room(db, code)      # fresh read, taken inside the lock
            if room.status != "LIVE" or not room.current_lot_id:
                return False, None
            lot = db.get(Lot, room.current_lot_id)
            if not lot or lot.status != "LIVE" or not lot.ends_at:
                return False, None
            if seconds_until(lot.ends_at) > 0:     # a bid extended the timer; not expired after all
                return False, lot.ends_at
            settle_lot(db, room)
            new_lot = open_next_lot(db, room, lead_in=result_pause_for(lot))
            room.version += 1                      # clients order/validate updates by version
            db.commit()
            snap = snapshot(db, room)
            publish(room.code, {"type": "snapshot", "data": snap})
            return True, (new_lot.ends_at if new_lot is not None else None)

async def _settle_and_rearm(code):
    try:
        settled, next_deadline = await asyncio.to_thread(_settle_expired_lot, code)
    except Exception:
        # Keep the durable state untouched if the database is temporarily unavailable;
        # try again shortly instead of leaving the player stuck.
        logger.exception("timer settlement failed room=%s; retrying in 3s", code)
        arm_timer(code, now_utc() + timedelta(seconds=3))
        return
    if next_deadline is not None:
        arm_timer(code, next_deadline)

async def timer_supervisor(code, deadline=None):
    """Sleep to the persisted deadline; never poll PostgreSQL several times per second."""
    try:
        if deadline is None:
            deadline = await asyncio.to_thread(_read_timer_deadline, code)
        if deadline is None:
            return
        await asyncio.sleep(max(0.0, seconds_until(deadline)))
        # Small arbitration window: a bid that reached the server just before the
        # deadline must be able to acquire the room lock and commit before settlement.
        # Eligibility is still decided from the request-arrival timestamp in validate_bid.
        await asyncio.sleep(max(0.0, float(os.getenv("BID_DEADLINE_ARBITRATION_MS", "650"))) / 1000.0)
        # A new bid re-arms (cancels) this task. Once settlement has started it must run to
        # completion and publish its result, so it runs shielded from that cancellation.
        await asyncio.shield(_settle_and_rearm(code))
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("timer supervisor error room=%s", code)

def arm_timer(code, deadline=None):
    """(Re)start the settlement timer. Pass the freshly committed deadline when known so no
    database read is needed; with deadline=None it is read from the database."""
    code = code.upper()
    task = timer_tasks.get(code)
    if task and not task.done():
        try:
            task.cancel()
        except RuntimeError:
            pass
    try:
        timer_tasks[code] = asyncio.create_task(timer_supervisor(code, deadline))
    except RuntimeError:
        pass

@app.on_event("startup")
async def recover_live_rooms():
    _ensure_loop()
    # Do not restart a live timer automatically. Recover the durable auction
    # state, pause the room, and let the host explicitly resume with a full timer.
    with SessionLocal() as db:
        recover_interrupted_rooms(db)

_ASSET_REF = re.compile(r'(/static/(?:app\.js|style\.css))(\?v=[^"\']*)?')

def _index_html():
    """index.html with every app.js/style.css reference stamped with a hash of the file's CONTENT,
    whatever query string the HTML had. Any change to the files changes the URL, so browsers cannot
    keep running a stale frontend after a deploy."""
    import hashlib
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    def stamp(m):
        f = STATIC / m.group(1).split("/static/")[1]
        digest = hashlib.sha1(f.read_bytes()).hexdigest()[:10] if f.exists() else "0"
        return f"{m.group(1)}?v={digest}"
    return _ASSET_REF.sub(stamp, html)

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store, max-age=0"})

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
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))   # no 0/O/1/I/L: read out loud and typed on phones
    host_token = new_password()
    room = Room(code=code, name=payload.name, host_token_hash=token_hash(host_token),
                purse=payload.purse, timer_seconds=payload.timer_seconds,
                min_squad=payload.min_squad, max_squad=payload.max_squad,
                bid_extension=payload.bid_extension)
    db.add(room)
    db.flush()
    invite_links = []
    for i, name in enumerate(names):
        token = new_password()
        f = Franchise(room_id=room.id, name=name, invite_hash=token_hash(token),
                      purse_remaining=payload.purse, created_order=i)
        db.add(f)
        db.flush()
        # `password` is what the franchise types at login. It is also the bearer credential, so the
        # bid path is unchanged. `url` keeps working as a direct-login link.
        invite_links.append({"franchise": name, "franchise_id": f.id, "password": token, "token": token,
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
    return {"room_code": code, "host_token": host_token, "host_password": host_token,
            "host_url": f"/?room={code}&token={host_token}",
            "franchise_invites": invite_links, "players_loaded": len(players)}

_login_failures: dict[str, list[float]] = {}

def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))

def _login_throttled(key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _login_failures.get(key, []) if now - t < 300]
    _login_failures[key] = recent
    if len(_login_failures) > 4096:
        _login_failures.clear()
    return len(recent) >= 15

class LoginRequest(BaseModel):
    room_code: str = Field(min_length=1, max_length=12)
    team: str | int            # franchise id, or "host"
    password: str = Field(min_length=1, max_length=200)

@app.get("/api/rooms/{code}/teams")
def room_teams(code: str, db: Session = Depends(get_db)):
    """Names only (no credentials): lets the login screen offer a team list for a room code."""
    room = db.scalar(select(Room).where(Room.code == code.strip().upper()))
    if room is None:
        raise HTTPException(404, "No auction found with that room code")
    rows = db.scalars(select(Franchise).where(Franchise.room_id == room.id).order_by(Franchise.created_order)).all()
    return {"room_name": room.name, "status": room.status, "teams": [{"id": f.id, "name": f.name} for f in rows]}

@app.post("/api/login")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Team + password -> the same bearer credential the invite links used. Nothing else about the
    bid path changes: after login every request authenticates exactly as before."""
    key = _client_key(request)
    if _login_throttled(key):
        raise HTTPException(429, "Too many wrong attempts. Wait a few minutes and try again.")
    hashed = token_hash(payload.password)
    code = payload.room_code.strip().upper()
    room = db.scalar(select(Room).where(Room.code == code))
    ok, franchise, is_host = False, None, False
    if room is not None:
        if str(payload.team).lower() == "host":
            ok = is_host = secrets.compare_digest(room.host_token_hash, hashed)
            if ok and room.host_franchise_id:
                franchise = db.get(Franchise, room.host_franchise_id)
        else:
            try:
                fid = int(payload.team)
            except ValueError:
                fid = None
            franchise = db.scalar(select(Franchise).where(Franchise.id == fid, Franchise.room_id == room.id)) if fid is not None else None
            ok = bool(franchise) and secrets.compare_digest(franchise.invite_hash, hashed)
    if not ok:
        _login_failures.setdefault(key, []).append(time.monotonic())
        raise HTTPException(401, "Wrong room code, team or password")
    _remember(code, hashed)
    return {"token": normalize_credential(payload.password), "room_code": code, "is_host": is_host,
            "franchise": ({"id": franchise.id, "name": franchise.name} if franchise else None)}

@app.post("/api/rooms/{code}/host/reset-password")
def reset_password(code: str, payload: dict, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """Host-only: issue a new password for one franchise (the old one stops working for new requests)."""
    room = require_host(db, code, authorization)
    f = db.scalar(select(Franchise).where(Franchise.id == payload.get("franchise_id"), Franchise.room_id == room.id))
    if f is None:
        raise HTTPException(404, "Franchise not found in this room")
    password = new_password()
    f.invite_hash = token_hash(password)
    db.commit()
    return {"franchise_id": f.id, "franchise": f.name, "password": password}

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

def _locked_host_mutation(code: str, authorization: str | None, mutate):
    """Run a host-only state change under the room lock (shared with bids and the timer).

    authenticate -> lock -> fresh read -> mutate -> version+1 -> commit -> full snapshot -> publish.
    The version bump and commit happen while the lock is held, so room versions are unique
    and strictly increasing, and the broadcast is queued in commit order."""
    with SessionLocal() as db:
        room, _franchise, is_host = auth_context(db, code, authorization)
        if not is_host:
            raise HTTPException(403, "Host permission required")
        with lock_for(room.code):
            db.refresh(room)
            try:
                mutate(db, room)
                room.version += 1
                db.commit()
            except ValueError as e:
                db.rollback()
                raise HTTPException(status_code=400, detail=str(e))
            snap = snapshot(db, room)
            publish(room.code, {"type": "snapshot", "data": snap})
            return snap

def _start_auction(code, authorization):
    def mutate(db, room):
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
    return _locked_host_mutation(code, authorization, mutate)

@app.post("/api/rooms/{code}/start")
async def start_auction(code: str, authorization: str | None = Header(default=None)):
    _ensure_loop()
    result = await asyncio.to_thread(_start_auction, code, authorization)
    arm_timer(code)
    return result

def _log_bid(**fields):
    logger.info("bid_timing " + " ".join(f"{k}={v}" for k, v in fields.items()))

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

def _is_dead_connection(exc: Exception) -> bool:
    return isinstance(exc, DBAPIError) and (bool(getattr(exc, "connection_invalidated", False)) or isinstance(exc, OperationalError))

def _read_with_retry(db, fn, *args):
    """Run a READ; if the pooled connection turns out to be dead, discard it and retry once on a fresh
    one. Used only before anything has been written, so a retry can never place a bid twice."""
    try:
        return fn(*args)
    except Exception as e:
        if not _is_dead_connection(e):
            raise
        logger.warning("db_connection_retry err=%s", type(e).__name__)
        try:
            db.rollback()
        except Exception:
            pass
        return fn(*args)

def _process_bid(code: str, authorization: str | None, req: BidRequest, request_id: str, received_at=None):
    """Authenticate, validate, write and COMMIT one bid; never touches a full snapshot.

    Returns (http_status, body, rearm_deadline). Round trips on the normal path (excluding the
    pool's pre-ping): one combined authenticate+read, then lot UPDATE, room UPDATE, bid INSERT,
    event INSERT, COMMIT. The bid is acknowledged only after COMMIT has returned."""
    t_start = time.perf_counter()
    code = code.upper()
    hashed = _bearer_hash(authorization)
    if (code, hashed) not in _verified:
        # First bid from this credential in this process: verify it BEFORE queueing for the
        # lock, so unauthenticated callers can never hold up real bidders.
        with SessionLocal() as pre:
            _read_with_retry(pre, auth_context, pre, code, authorization)
    t_auth = time.perf_counter()
    timings = {}
    base = dict(request_id=request_id, room=code, submitted=req.amount)
    perf.begin()
    traced = True
    try:
        with SessionLocal() as db:
            t_wait = time.perf_counter()
            with lock_for(code):
                t_locked = time.perf_counter()
                ctx = None
                wrote = False
                committing = False
                try:
                    ctx = _read_with_retry(db, load_bid_context, db, code, hashed)   # authenticate + read state (1 round trip)
                    t_read = time.perf_counter()
                    validate_bid(ctx, req.amount, lot_id=req.lot_id, player_id=req.player_id,
                                 franchise_id=req.franchise_id, received_at=received_at)
                    t_valid = time.perf_counter()
                    wrote = True
                    accepted = apply_bid(db, ctx, req.amount)
                    t_write = time.perf_counter()
                    committing = True
                    db.commit()
                    committing = False
                    t_commit = time.perf_counter()
                    # Build the response from the values just committed. No further reads.
                    body = dict(accepted, accepted=True, request_id=request_id,
                                your_franchise_id=ctx.franchise.id)
                    t_resp = time.perf_counter()
                    publish(code, accepted)                          # queued in commit order; cannot block
                    t_pub = time.perf_counter()
                    n, sql = perf.summarise(perf.end()); traced = False
                    _log_bid(**base, outcome="accepted", accepted_amount=req.amount,
                             version=accepted["room_version"],
                             auth_ms=f"{(t_auth-t_start)*1000:.1f}", lock_wait_ms=f"{(t_locked-t_wait)*1000:.1f}",
                             read_auth_ms=f"{(t_read-t_locked)*1000:.1f}",
                             validate_ms=f"{(t_valid-t_read)*1000:.1f}",
                             write_ms=f"{(t_write-t_valid)*1000:.1f}", commit_ms=f"{(t_commit-t_write)*1000:.1f}",
                             response_ms=f"{(t_resp-t_commit)*1000:.1f}", broadcast_enqueue_ms=f"{(t_pub-t_resp)*1000:.1f}",
                             total_ms=f"{(t_pub-t_start)*1000:.1f}", db_stmts=n, **({"sql": sql} if sql else {}))
                    return 200, body, ctx.lot.ends_at   # new committed deadline (re-arms the timer without a DB read)
                except BidRejected as rej:
                    state = compact_state(ctx) if ctx is not None else None   # before any rollback expires ctx
                    rearm = ctx.lot.ends_at if (ctx is not None and rej.code == "TIMER_EXPIRED") else None
                    if wrote:
                        db.rollback()
                    n, sql = perf.summarise(perf.end()); traced = False
                    _log_bid(**base, outcome="rejected", reason=rej.code,
                             version=(state or {}).get("room_version", ""),
                             auth_ms=f"{(t_auth-t_start)*1000:.1f}", lock_wait_ms=f"{(t_locked-t_wait)*1000:.1f}",
                             total_ms=f"{(time.perf_counter()-t_start)*1000:.1f}", db_stmts=n)
                    body = {"accepted": False, "code": rej.code, "detail": rej.message,
                            "request_id": request_id, "submitted_amount": req.amount, "state": state}
                    return rej.http_status, body, rearm
                except AuthError as e:
                    raise HTTPException(e.status, e.message)
                except HTTPException:
                    raise
                except Exception as e:
                    # Nothing is acknowledged and nothing is broadcast. The database is authoritative.
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    code_ = "BID_NOT_CONFIRMED" if committing else "BID_NOT_SAVED"
                    logger.exception("bid_failed request_id=%s room=%s reason=%s err=%s", request_id, code, code_, type(e).__name__)
                    msg = ("Your bid could not be confirmed. Check the current price before bidding again."
                           if committing else "Your bid was NOT placed (server error). Please try again.")
                    return 503, {"accepted": False, "code": code_, "detail": msg,
                                 "request_id": request_id, "submitted_amount": req.amount, "state": None}, None
    finally:
        if traced:
            perf.end()

@app.post("/api/rooms/{code}/bid")
async def bid(code: str, payload: BidRequest, authorization: str | None = Header(default=None),
              x_request_id: str | None = Header(default=None)):
    _ensure_loop()
    request_id = x_request_id if x_request_id and _REQUEST_ID.match(x_request_id) else uuid.uuid4().hex[:12]
    # SQLAlchemy uses a synchronous psycopg connection. Run the whole transaction
    # off the event loop so it cannot stall WebSockets, timers, or other bidders.
    received_at = now_utc()  # capture before DB work; deadline eligibility is based on arrival, not commit latency
    status, body, rearm = await asyncio.to_thread(_process_bid, code, authorization, payload, request_id, received_at)
    if rearm is not None:
        arm_timer(code, rearm)   # accepted: new deadline; TIMER_EXPIRED: settle immediately
    if status == 200:
        return body
    return JSONResponse(body, status_code=status)

def _select_host_franchise(code, authorization, franchise_id):
    def mutate(db, room):
        if room.status != "SETUP":
            raise HTTPException(400, "Choose the host's franchise before the auction starts")
        franchise = db.scalar(select(Franchise).where(Franchise.id == franchise_id, Franchise.room_id == room.id)) if franchise_id else None
        if franchise_id and not franchise:
            raise HTTPException(404, "Franchise not found in this room")
        room.host_franchise_id = franchise.id if franchise else None
    return _locked_host_mutation(code, authorization, mutate)

@app.post("/api/rooms/{code}/host/select-franchise")
async def select_host_franchise(code: str, payload: dict, authorization: str | None = Header(default=None)):
    _ensure_loop()
    return await asyncio.to_thread(_select_host_franchise, code, authorization, payload.get("franchise_id"))

def _apply_host_action(db, room, action):
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
                open_next_lot(db, room, lead_in=result_pause_for(lot))
                return
        open_next_lot(db, room)
    elif action == "undo-sale":
        undo_last_sale(db, room)
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

def _process_host_action(code: str, action: str, authorization: str | None):
    return _locked_host_mutation(code, authorization, lambda db, room: _apply_host_action(db, room, action))

@app.post("/api/rooms/{code}/host/{action}")
async def host_action(code: str, action: str, authorization: str | None = Header(default=None)):
    _ensure_loop()
    result = await asyncio.to_thread(_process_host_action, code, action, authorization)
    if action in ("resume", "advance", "undo-bid", "redo-bid", "undo-sale", "redo-sale") and result.get("room", {}).get("status") == "LIVE":
        arm_timer(code)
    elif action in ("pause", "finish") or result.get("room", {}).get("status") == "PAUSED":
        task = timer_tasks.get(code.upper())
        if task:
            task.cancel()
    return result

def _nominate(code, authorization, payload):
    def mutate(db, room):
        nominated_unsold(db, room, payload.player_ids, payload.price_overrides)
    return _locked_host_mutation(code, authorization, mutate)

@app.post("/api/rooms/{code}/nominate-unsold")
async def nominate(code: str, payload: NominationRequest, authorization: str | None = Header(default=None)):
    _ensure_loop()
    result = await asyncio.to_thread(_nominate, code, authorization, payload)
    if result["room"]["status"] == "LIVE":
        arm_timer(code)
    return result

def _ws_authorised(code, token):
    try:
        with SessionLocal() as db:
            auth_context(db, code, "Bearer " + token)
        return True
    except Exception:
        return False

@app.websocket("/ws/{code}")
async def websocket_endpoint(ws: WebSocket, code: str, token: str):
    _ensure_loop()
    if not await asyncio.to_thread(_ws_authorised, code, token):
        await ws.close(code=1008)
        return
    conn = await manager.connect(code, ws)
    try:
        # State recovery for THIS client only (join / reconnect). Other clients are not
        # sent anything: previously every reconnect pushed a full snapshot to the whole room.
        try:
            snap = await asyncio.to_thread(_snapshot_for_room, code)
            conn.send(json.dumps({"type": "snapshot", "reason": "connect", "data": snap}, separators=(",", ":")))
        except Exception:
            logger.exception("initial snapshot failed room=%s", code)
        while True:
            # Browser clients send an application-level heartbeat so hosted
            # proxies can detect and preserve otherwise-idle WebSocket sessions.
            # Auction mutations still only use authenticated HTTP endpoints.
            message = await ws.receive_text()
            if message == "ping" or message == '{"type":"ping"}':
                conn.send('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(code, conn)

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

