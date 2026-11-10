import queue, threading, time
from contextlib import contextmanager
from fastapi.testclient import TestClient
from sqlalchemy import select, func, event as sa_event

import app.main as main
from app.db import SessionLocal, engine
from app.models import Room, Lot, Bid, Sale, Franchise, Event
from app.engine import now_utc
from datetime import timedelta

PG = engine.dialect.name == "postgresql"


class Auction:
    def __init__(self, client, data, franchise_names):
        self.client = client
        self.code = data["room_code"]
        self.host_token = data["host_token"]
        self.tokens = {x["franchise"]: x["token"] for x in data["franchise_invites"]}
        self.names = franchise_names

    def H(self, who):
        tok = self.host_token if who == "HOST" else self.tokens[who]
        return {"Authorization": "Bearer " + tok}

    def snap(self, who="HOST"):
        r = self.client.get(f"/api/rooms/{self.code}/snapshot", headers=self.H(who))
        assert r.status_code == 200, r.text
        return r.json()

    def current(self):
        return self.snap()["current_player"]

    def bid(self, who, amount=None, lot_id="auto", **extra):
        cp = self.current() if (amount is None or lot_id == "auto") else None
        if amount is None:
            amount = cp["next_bid"]
        body = {"amount": amount}
        if lot_id == "auto":
            lot_id = cp["lot_id"]
        if lot_id is not None:
            body["lot_id"] = lot_id
        body.update(extra)
        return self.client.post(f"/api/rooms/{self.code}/bid", headers=self.H(who), json=body)

    def host(self, action):
        return self.client.post(f"/api/rooms/{self.code}/host/{action}", headers=self.H("HOST"))

    # --- direct database access (independent session) ---
    def db_lot(self):
        with SessionLocal() as db:
            room = db.scalar(select(Room).where(Room.code == self.code))
            lot = db.get(Lot, room.current_lot_id) if room.current_lot_id else None
            return room, lot

    def set_deadline(self, seconds_from_now):
        with SessionLocal() as db:
            room = db.scalar(select(Room).where(Room.code == self.code))
            lot = db.get(Lot, room.current_lot_id)
            lot.ends_at = now_utc() + timedelta(seconds=seconds_from_now)
            db.commit()

    def count(self, model, **where):
        with SessionLocal() as db:
            room = db.scalar(select(Room).where(Room.code == self.code))
            q = select(func.count(model.id)).where(model.room_id == room.id)
            for k, v in where.items():
                q = q.where(getattr(model, k) == v)
            return db.scalar(q)

    def purse(self, name):
        with SessionLocal() as db:
            room = db.scalar(select(Room).where(Room.code == self.code))
            return db.scalar(select(Franchise.purse_remaining).where(Franchise.room_id == room.id, Franchise.name == name))


def make_auction(client, franchises=("MI", "CSK", "RCB"), purse=5000, min_squad=1, max_squad=25,
                 timer=25, players=6, base=100, extension=True, start=True, host_franchise=None):
    payload = {"name": "T", "franchises": list(franchises), "purse": purse, "timer_seconds": timer,
               "min_squad": min_squad, "max_squad": max_squad, "bid_extension": extension,
               "players": [{"name": f"Player {i}", "state_team": "X", "role": "BAT", "base_price": base,
                            "importance_weight": 1.0} for i in range(players)]}
    r = client.post("/api/rooms", json=payload)
    assert r.status_code == 200, r.text
    a = Auction(client, r.json(), list(franchises))
    if host_franchise:
        fid = next(f["id"] for f in a.snap()["franchises"] if f["name"] == host_franchise)
        r = client.post(f"/api/rooms/{a.code}/host/select-franchise", headers=a.H("HOST"), json={"franchise_id": fid})
        assert r.status_code == 200, r.text
    if start:
        r = client.post(f"/api/rooms/{a.code}/start", headers=a.H("HOST"))
        assert r.status_code == 200, r.text
    return a


class WSProbe:
    """A WebSocket client whose messages are collected on a queue so tests can assert with timeouts."""
    def __init__(self, client, code, token):
        self._cm = client.websocket_connect(f"/ws/{code}?token={token}")
        self.ws = self._cm.__enter__()
        self.q = queue.Queue()
        self.seen = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            while True:
                self.q.put(self.ws.receive_json())
        except BaseException:
            self.q.put({"type": "__closed__"})

    def next(self, timeout=4.0):
        m = self.q.get(timeout=timeout)
        self.seen.append(m)
        return m

    def until(self, type_, timeout=4.0, min_version=None):
        end = time.time() + timeout
        while True:
            m = self.next(max(0.05, end - time.time()))
            if m["type"] == type_ and (min_version is None or m.get("room_version", m.get("data", {}).get("room", {}).get("version", 0)) >= min_version):
                return m

    def drain(self, quiet=0.4):
        out = []
        while True:
            try:
                m = self.q.get(timeout=quiet)
                self.seen.append(m)
                out.append(m)
            except queue.Empty:
                return out

    def close(self):
        try:
            self._cm.__exit__(None, None, None)
        except BaseException:
            pass


@contextmanager
def slow_commits(seconds):
    """Inject latency into every COMMIT (models the network round trip to the database)."""
    flag = {"on": True}

    @sa_event.listens_for(engine, "commit")
    def _slow(conn):
        if flag["on"]:
            time.sleep(seconds)
    try:
        yield
    finally:
        flag["on"] = False
        sa_event.remove(engine, "commit", _slow)
