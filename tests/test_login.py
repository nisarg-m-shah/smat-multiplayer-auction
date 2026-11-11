"""Team + password login, host password reset, and the dead-connection retry on the bid path."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.main as main
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _creds(client):
    r = client.post("/api/rooms", json={"name": "T", "franchises": ["MI", "CSK"], "purse": 5000, "timer_seconds": 25,
                                        "min_squad": 1, "max_squad": 25,
                                        "players": [{"name": f"P{i}", "state_team": "X", "role": "BAT", "base_price": 100} for i in range(3)]})
    assert r.status_code == 200, r.text
    return r.json()


def test_creation_returns_short_readable_passwords(client):
    d = _creds(client)
    pw = d["franchise_invites"][0]["password"]
    assert len(pw) == 11 and pw[5] == "-" and d["host_password"] == d["host_token"]
    assert all(c not in pw for c in "01OIL")


def test_login_with_team_and_password_then_bid_path_is_unchanged(client):
    d = _creds(client)
    code = d["room_code"]
    teams = client.get(f"/api/rooms/{code.lower()}/teams").json()["teams"]
    mi = next(t for t in teams if t["name"] == "MI")
    pw = next(x["password"] for x in d["franchise_invites"] if x["franchise"] == "MI")
    # typed sloppily: lower case, no dash, spaces
    sloppy = pw.replace("-", " ").lower()
    r = client.post("/api/login", json={"room_code": code.lower(), "team": mi["id"], "password": sloppy})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["franchise"]["name"] == "MI" and not body["is_host"]
    tok = body["token"]
    client.post(f"/api/rooms/{code}/start", headers={"Authorization": "Bearer " + d["host_token"]})
    snap = client.get(f"/api/rooms/{code}/snapshot", headers={"Authorization": "Bearer " + tok}).json()
    lot = snap["current_player"]
    rb = client.post(f"/api/rooms/{code}/bid", headers={"Authorization": "Bearer " + tok},
                     json={"amount": lot["next_bid"], "lot_id": lot["lot_id"]})
    assert rb.status_code == 200 and rb.json()["leading_franchise_name"] == "MI"


def test_host_login_and_wrong_credentials(client):
    d = _creds(client)
    code = d["room_code"]
    assert client.post("/api/login", json={"room_code": code, "team": "host", "password": d["host_password"]}).json()["is_host"] is True
    cski = next(x for x in d["franchise_invites"] if x["franchise"] == "CSK")
    # a franchise password does not open another team or the host seat
    assert client.post("/api/login", json={"room_code": code, "team": "host", "password": cski["password"]}).status_code == 401
    mi_id = next(x["franchise_id"] for x in d["franchise_invites"] if x["franchise"] == "MI")
    assert client.post("/api/login", json={"room_code": code, "team": mi_id, "password": cski["password"]}).status_code == 401
    assert client.post("/api/login", json={"room_code": "ZZZZZZ", "team": mi_id, "password": "x"}).status_code == 401
    assert client.get("/api/rooms/ZZZZZZ/teams").status_code == 404


def test_login_is_throttled_after_repeated_failures(client):
    main._login_failures.clear()
    d = _creds(client)
    for _ in range(15):
        assert client.post("/api/login", json={"room_code": d["room_code"], "team": "host", "password": "WRONG"}).status_code == 401
    assert client.post("/api/login", json={"room_code": d["room_code"], "team": "host", "password": d["host_password"]}).status_code == 429
    main._login_failures.clear()


def test_legacy_long_invite_tokens_still_authenticate(client):
    # Rooms created before this release hold 43-char case-sensitive tokens.
    from app.security import new_token, token_hash
    from app.db import SessionLocal
    from app.models import Franchise, Room
    from sqlalchemy import select
    d = _creds(client)
    legacy = new_token()
    with SessionLocal() as db:
        room = db.scalar(select(Room).where(Room.code == d["room_code"]))
        f = db.scalar(select(Franchise).where(Franchise.room_id == room.id, Franchise.name == "MI"))
        f.invite_hash = token_hash(legacy)
        db.commit()
    r = client.get(f"/api/rooms/{d['room_code']}/me", headers={"Authorization": "Bearer " + legacy})
    assert r.status_code == 200 and r.json()["franchise"]["name"] == "MI"
    assert client.get(f"/api/rooms/{d['room_code']}/me", headers={"Authorization": "Bearer " + legacy.lower()}).status_code == 401


def test_host_can_reset_a_team_password(client):
    d = _creds(client)
    code, host = d["room_code"], {"Authorization": "Bearer " + d["host_token"]}
    mi = next(x for x in d["franchise_invites"] if x["franchise"] == "MI")
    new = client.post(f"/api/rooms/{code}/host/reset-password", headers=host, json={"franchise_id": mi["franchise_id"]}).json()["password"]
    assert new != mi["password"]
    assert client.post("/api/login", json={"room_code": code, "team": mi["franchise_id"], "password": mi["password"]}).status_code == 401
    assert client.post("/api/login", json={"room_code": code, "team": mi["franchise_id"], "password": new}).status_code == 200
    # franchises cannot reset passwords
    assert client.post(f"/api/rooms/{code}/host/reset-password", headers={"Authorization": "Bearer " + mi["password"]},
                       json={"franchise_id": mi["franchise_id"]}).status_code in (401, 403)


def test_bid_survives_one_dead_pooled_connection(client, monkeypatch):
    a = make_auction(client)
    real = main.load_bid_context
    calls = {"n": 0}

    def flaky(db, code, hashed):
        calls["n"] += 1
        if calls["n"] == 1:
            err = OperationalError("SELECT ...", {}, Exception("server closed the connection unexpectedly"))
            err.connection_invalidated = True
            raise err
        return real(db, code, hashed)

    monkeypatch.setattr(main, "load_bid_context", flaky)
    r = a.bid("MI", 100)
    assert r.status_code == 200, r.text
    assert calls["n"] == 2
    assert a.count(main.Bid) == 1, "retry must never double-place a bid"


def test_non_connection_errors_are_not_retried(client, monkeypatch):
    a = make_auction(client)
    calls = {"n": 0}

    def broken(db, code, hashed):
        calls["n"] += 1
        raise ValueError("boom")

    monkeypatch.setattr(main, "load_bid_context", broken)
    r = a.bid("MI", 100)
    assert r.status_code == 503 and calls["n"] == 1
