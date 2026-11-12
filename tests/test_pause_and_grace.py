"""Undo/redo while paused, pause timer floor, late-bid grace."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.models import Lot, Sale
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_undo_and_redo_bid_work_while_paused_and_stay_paused(client):
    a = make_auction(client, timer=10, base=100)
    a.bid("MI", 100); a.bid("CSK", 110)
    assert a.host("pause").status_code == 200
    r = a.host("undo-bid"); assert r.status_code == 200, r.text
    s = a.snap()
    assert s["room"]["status"] == "PAUSED" and s["current_player"]["current_bid"] == 100
    assert s["current_player"]["ends_at"] is None and s["current_player"]["remaining_ms"] == 10000
    assert a.host("redo-bid").status_code == 200
    s = a.snap()
    assert s["room"]["status"] == "PAUSED" and s["current_player"]["current_bid"] == 110
    assert a.host("resume").status_code == 200
    assert a.snap()["room"]["status"] == "LIVE"


def test_pause_never_leaves_less_than_half_the_timer(client):
    a = make_auction(client, timer=10, base=100)
    a.bid("MI", 100)
    a.set_deadline(0.3)
    a.host("pause")
    assert a.snap()["current_player"]["remaining_ms"] >= 5000
    a.host("resume")
    _, lot = a.db_lot()
    from app.engine import seconds_until
    assert 4.0 < seconds_until(lot.ends_at) <= 5.2


def test_pause_keeps_more_than_half_when_more_remains(client):
    a = make_auction(client, timer=10, base=100)
    a.bid("MI", 100)
    a.set_deadline(9.0)
    a.host("pause")
    assert 8000 <= a.snap()["current_player"]["remaining_ms"] <= 9100


def test_undo_last_player_while_paused_stays_paused(client):
    a = make_auction(client, players=3, base=100)
    a.bid("MI", 100); a.set_deadline(-1)
    main._settle_expired_lot(a.code)
    a.host("pause")
    assert a.host("undo-sale").status_code == 200
    assert a.snap()["room"]["status"] == "PAUSED"
    assert a.host("redo-sale").status_code == 200
    s = a.snap()
    assert s["room"]["status"] == "PAUSED"      # next player opened, but still paused


def test_late_bid_inside_grace_is_accepted_and_outside_is_not(client, monkeypatch):
    monkeypatch.setattr(main.config, "BID_GRACE_SECONDS", 0.8)
    monkeypatch.setattr(main.config, "SETTLE_SLACK_SECONDS", 0.25)
    a = make_auction(client, base=100)
    a.set_deadline(-0.3)
    r = a.bid("MI", 100); assert r.status_code == 200, r.text
    a.bid("CSK", 110)
    a.set_deadline(-1.5)
    r = a.bid("RCB", 120); assert r.status_code == 409 and r.json()["code"] == "TIMER_EXPIRED"


def test_settlement_waits_for_the_grace_window(client, monkeypatch):
    monkeypatch.setattr(main.config, "BID_GRACE_SECONDS", 0.8)
    monkeypatch.setattr(main.config, "SETTLE_SLACK_SECONDS", 0.25)
    a = make_auction(client, base=100)
    a.bid("MI", 100)
    a.set_deadline(-0.4)
    settled, nxt, hold = main._settle_expired_lot(a.code)
    assert not settled and a.count(Sale, reversed=False) == 0
    a.set_deadline(-1.5)
    settled, nxt, hold = main._settle_expired_lot(a.code)
    assert settled and a.count(Sale, reversed=False) == 1


def test_arrival_time_not_processing_time_decides(client):
    """A request that ARRIVED before the deadline wins even if it is processed after it."""
    from datetime import timedelta
    from app.engine import now_utc
    a = make_auction(client, base=100)
    import time; time.sleep(0.4)                 # let the freshly armed timer read its 25s deadline first
    a.set_deadline(-0.5)                         # deadline already 0.5s ago when processing happens
    _, lot = a.db_lot()
    arrived = lot.ends_at.replace(tzinfo=lot.ends_at.tzinfo or __import__("datetime").timezone.utc) - timedelta(seconds=0.1)
    cp = a.current()
    from app.main import BidRequest, _process_bid
    status, body, _ = _process_bid(a.code, "Bearer " + a.tokens["MI"], BidRequest(amount=cp["next_bid"], lot_id=cp["lot_id"]), "t1", arrived)
    assert status == 200, body


def test_time_endpoint(client):
    r = client.get("/api/time").json()
    import time
    assert abs(r["t"] - time.time() * 1000) < 2000


def test_snapshot_enables_bid_history_buttons_while_paused(client):
    a = make_auction(client, base=100)
    a.bid("MI", 100); a.host("pause")
    h = a.snap()["history"]
    assert h["can_undo_bid"]
    a.host("undo-bid")
    assert a.snap()["history"]["can_redo_bid"]


def test_unsold_list_includes_state_team(client):
    a = make_auction(client, players=3, base=100)
    a.set_deadline(-1)
    main._settle_expired_lot(a.code)          # nobody bid: unsold
    u = a.snap()["unsold"]
    assert u and "state_team" in u[0]
