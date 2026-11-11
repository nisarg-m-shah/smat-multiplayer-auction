"""The next player must not open until the SOLD / UNSOLD result has been shown."""
import asyncio
import app.main as main
from helpers import make_auction

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c



def test_next_player_waits_for_the_result_hold(client, monkeypatch):
    monkeypatch.setattr(main.config, "RESULT_HOLD_SOLD", 5.0)
    monkeypatch.setattr(main.config, "RESULT_HOLD_UNSOLD", 3.0)
    a = make_auction(client, players=3, base=100)
    sold_lot = a.current()["lot_id"]
    a.bid("MI", 100)
    a.set_deadline(-1)
    settled, nxt, hold = main._settle_expired_lot(a.code)
    assert settled and nxt is None and hold == 5.0
    s = a.snap()
    assert s["room"]["status"] == "BETWEEN_LOTS" and s["current_player"] is None
    assert any(e["kind"] == "SOLD" and e["data"]["player_name"] for e in s["events"])
    # nobody can bid while the result is showing
    r = a.bid("CSK", 110, lot_id=sold_lot)
    assert r.status_code == 409
    assert a.purse("MI") == 5000 - 100
    # after the hold the next player opens with a full timer
    deadline = main._open_after_hold(a.code)
    assert deadline is not None
    s = a.snap()
    assert s["room"]["status"] == "LIVE" and s["current_player"]["lot_id"] != sold_lot
    # opening twice is harmless
    assert main._open_after_hold(a.code) is None


def test_unsold_uses_its_own_shorter_hold(client, monkeypatch):
    monkeypatch.setattr(main.config, "RESULT_HOLD_SOLD", 5.0)
    monkeypatch.setattr(main.config, "RESULT_HOLD_UNSOLD", 3.0)
    a = make_auction(client, players=3, base=100)
    a.set_deadline(-1)
    _, _, hold = main._settle_expired_lot(a.code)
    assert hold == 3.0


def test_zero_hold_opens_immediately(client):
    a = make_auction(client, players=3, base=100)
    first = a.current()["lot_id"]
    a.set_deadline(-1)
    _, nxt, hold = main._settle_expired_lot(a.code)
    assert hold == 0 and nxt is not None and a.current()["lot_id"] != first


def test_host_nomination_or_undo_during_hold_cancels_the_pending_open(client, monkeypatch):
    monkeypatch.setattr(main.config, "RESULT_HOLD_SOLD", 5.0)
    a = make_auction(client, players=4, base=100)
    first = a.current()["lot_id"]
    a.bid("MI", 100); a.set_deadline(-1); main._settle_expired_lot(a.code)
    assert a.host("undo-sale").status_code == 200
    assert a.current()["lot_id"] == first
    # a late "open after hold" must not push Samson off the block
    assert main._open_after_hold(a.code) is None
    assert a.current()["lot_id"] == first


def test_restart_during_hold_recovers_with_next_player_paused(client, monkeypatch):
    from app.db import SessionLocal
    from app.engine import recover_interrupted_rooms
    monkeypatch.setattr(main.config, "RESULT_HOLD_SOLD", 5.0)
    a = make_auction(client, players=3, base=100)
    a.bid("MI", 100); a.set_deadline(-1); main._settle_expired_lot(a.code)
    with SessionLocal() as db:
        recover_interrupted_rooms(db)
    s = a.snap()
    assert s["room"]["status"] == "PAUSED" and s["current_player"] is not None
    assert a.host("resume").status_code == 200
