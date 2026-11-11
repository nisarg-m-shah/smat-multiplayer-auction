"""Undo last player must put him back at his final bid, even when the next player has already started."""
import pytest
from sqlalchemy import select
import app.main as main
from app.db import SessionLocal
from app.models import Lot, Player, Sale, Bid
from helpers import make_auction

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c



def _settle(a):
    a.set_deadline(-1)
    return main._settle_expired_lot(a.code)


def _names(a):
    return [e["message"] for e in a.snap()["events"]]


def test_undo_after_next_player_started_restores_last_highest_bid(client):
    a = make_auction(client, players=4, base=100)
    samson = a.current()
    assert a.bid("MI", 100).status_code == 200
    assert a.bid("CSK", 110).status_code == 200
    assert a.bid("RCB", 120).status_code == 200
    _settle(a)                                    # Samson sold to RCB @120, next player opens
    jaiswal = a.current()
    assert jaiswal["lot_id"] != samson["lot_id"]
    assert a.bid("MI", 100).status_code == 200    # bidding has already started on Jaiswal
    assert a.purse("RCB") == 5000 - 120

    r = a.host("undo-sale")
    assert r.status_code == 200, r.text
    snap = r.json()
    cp = snap["current_player"]
    # Samson is back, at the last highest bid, with the same leader
    assert cp["lot_id"] == samson["lot_id"] and cp["name"] == samson["name"]
    assert cp["current_bid"] == 120 and cp["highest_franchise"] == "RCB"
    assert a.purse("RCB") == 5000                 # sale reversed
    assert snap["room"]["status"] == "LIVE"
    # the lone existing bidder cannot out-bid himself, others continue from 120
    assert a.bid("RCB").status_code in (400, 409)
    assert a.bid("MI", 130).status_code == 200

    # Samson is sold again (to MI @130); Jaiswal -- and only Jaiswal -- comes next, fresh at base price
    _settle(a)
    nxt = a.current()
    assert nxt["lot_id"] == jaiswal["lot_id"] and nxt["name"] == jaiswal["name"]
    assert nxt["current_bid"] == 0 and nxt["highest_franchise"] is None
    assert a.purse("MI") == 5000 - 130
    assert a.count(Sale, reversed=False) == 1     # only the re-sale to MI stands


def test_undo_during_result_hold_restores_last_bid(client, monkeypatch):
    monkeypatch.setattr(main.config, "RESULT_HOLD_SOLD", 30.0)
    a = make_auction(client, players=3, base=100)
    samson = a.current()
    a.bid("MI", 100); a.bid("CSK", 110)
    settled, _, hold = _settle(a)
    assert settled and hold == 30.0
    assert a.snap()["room"]["status"] == "BETWEEN_LOTS" and a.snap()["current_player"] is None
    snap = a.host("undo-sale").json()
    assert snap["current_player"]["lot_id"] == samson["lot_id"]
    assert (snap["current_player"]["current_bid"], snap["current_player"]["highest_franchise"]) == (110, "CSK")


def test_undo_unsold_player_brings_him_back_at_base(client):
    a = make_auction(client, players=3, base=100)
    first = a.current()
    _settle(a)                                    # unsold
    second = a.current()
    snap = a.host("undo-sale").json()
    assert snap["current_player"]["lot_id"] == first["lot_id"] and snap["current_player"]["current_bid"] == 0
    _settle(a)
    assert a.current()["lot_id"] == second["lot_id"]


def test_displaced_player_loses_his_bids_but_keeps_his_turn(client):
    a = make_auction(client, players=4, base=100)
    first = a.current(); a.bid("MI", 100); _settle(a)
    second = a.current(); a.bid("CSK", 100); a.bid("RCB", 110)
    a.host("undo-sale")
    _settle(a)                                    # first sold again to MI
    again = a.current()
    assert again["lot_id"] == second["lot_id"] and again["current_bid"] == 0
    assert a.purse("RCB") == 5000 and a.purse("CSK") == 5000
    with SessionLocal() as db:
        assert not db.scalars(select(Bid).where(Bid.lot_id == second["lot_id"], Bid.active == True)).all()  # lot-scoped


def test_paused_room_stays_paused_after_undo(client):
    a = make_auction(client, players=3, base=100)
    a.bid("MI", 100); _settle(a)
    assert a.host("pause").status_code == 200
    snap = a.host("undo-sale").json()
    assert snap["room"]["status"] == "PAUSED" and snap["current_player"]["current_bid"] == 100
    assert a.host("resume").status_code == 200
    assert a.snap()["room"]["status"] == "LIVE"


def test_redo_player_after_undo_resells_at_original_price(client):
    a = make_auction(client, players=3, base=100)
    a.bid("MI", 100); a.bid("CSK", 110); _settle(a)
    a.host("undo-sale")
    assert a.snap()["history"]["can_redo_sale"] is True
    assert a.host("redo-sale").status_code == 200
    assert a.purse("CSK") == 5000 - 110


def test_redo_player_is_refused_once_bidding_moved_on(client):
    a = make_auction(client, players=3, base=100)
    a.bid("MI", 100); a.bid("CSK", 110); _settle(a)
    a.host("undo-sale")
    assert a.bid("RCB", 120).status_code == 200
    assert a.snap()["history"]["can_redo_sale"] is False
    assert a.host("redo-sale").status_code == 400
    assert a.purse("CSK") == 5000                 # nothing was applied


def test_undo_twice_unwinds_two_players(client):
    a = make_auction(client, players=4, base=100)
    p1 = a.current(); a.bid("MI", 100); _settle(a)
    p2 = a.current(); a.bid("CSK", 100); _settle(a)
    p3 = a.current()
    a.host("undo-sale")                           # p2 back
    assert a.current()["lot_id"] == p2["lot_id"]
    a.host("undo-sale")                           # p1 back, p2 returns to queue
    assert a.current()["lot_id"] == p1["lot_id"] and a.current()["current_bid"] == 100
    _settle(a)
    assert a.current()["lot_id"] == p2["lot_id"]  # order preserved: p2 then p3
    _settle(a)
    assert a.current()["lot_id"] == p3["lot_id"]
