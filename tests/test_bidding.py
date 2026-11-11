"""Exact-amount bidding, stale-bid rejection, concurrency, timer coordination (Tests A-F, J)."""
import threading, time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.main as main
import app.engine as engine_mod
from app.models import Bid, Sale, Event
from helpers import make_auction, WSProbe, slow_commits, PG


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------- Test A
def test_a_normal_bid_is_accepted_with_compact_confirmation(client):
    a = make_auction(client)
    cp = a.current()
    assert cp["next_bid"] == 100
    v0 = a.snap()["room"]["version"]

    r = a.bid("MI", 100)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["type"] == "BID_ACCEPTED" and d["accepted"] is True
    assert d["highest_bid"] == 100
    assert d["leading_franchise_name"] == "MI"
    assert d["next_bid_amount"] == 110
    assert d["room_version"] == v0 + 1
    assert d["lot_id"] == cp["lot_id"] and d["player_name"] == cp["name"]
    assert d["timer_deadline"].endswith("Z")
    # compact: no squads / players / franchise lists
    assert "franchises" not in d and "data" not in d and len(str(d)) < 800
    # ...and it matches what was durably committed
    s = a.snap()
    assert s["current_player"]["current_bid"] == 100
    assert s["current_player"]["highest_franchise"] == "MI"
    assert s["room"]["version"] == d["room_version"]
    assert a.count(Bid) == 1


# ---------------------------------------------------------------- Test B + exact amount
def test_b_stale_bid_is_rejected_and_never_escalated(client):
    a = make_auction(client)
    assert a.bid("MI", 100).status_code == 200
    r = a.bid("CSK", 100)            # CSK still believes the price is 100
    assert r.status_code == 409
    d = r.json()
    assert d["code"] == "STALE_BID" and d["accepted"] is False
    assert isinstance(d["detail"], str) and "110" in d["detail"]
    st = d["state"]
    assert st["highest_bid"] == 100 and st["leading_franchise_name"] == "MI"
    assert st["next_bid_amount"] == 110
    # NOT silently converted into 110
    room, lot = a.db_lot()
    assert lot.current_bid == 100 and a.count(Bid) == 1
    assert a.purse("CSK") == 5000
    # CSK now consciously bids 110
    r = a.bid("CSK", 110)
    assert r.status_code == 200 and r.json()["highest_bid"] == 110
    assert a.count(Bid) == 2


def test_amount_must_equal_required_price_exactly(client):
    a = make_auction(client)
    assert a.bid("MI", 100).status_code == 200
    # lower and HIGHER than required are both rejected; the server never adjusts either
    for amount in (90, 100, 120, 500):
        r = a.bid("CSK", amount)
        assert r.status_code == 409 and r.json()["code"] == "STALE_BID", (amount, r.text)
    assert a.db_lot()[1].current_bid == 100
    assert a.count(Bid) == 1


def test_amount_is_required(client):
    a = make_auction(client)
    r = client.post(f"/api/rooms/{a.code}/bid", headers=a.H("MI"), json={})
    assert r.status_code == 422
    r = client.post(f"/api/rooms/{a.code}/bid", headers=a.H("MI"), json={"amount": None})
    assert r.status_code == 422
    assert a.count(Bid) == 0


# ---------------------------------------------------------------- authorisation
def test_credentials_and_franchise_identity(client):
    a = make_auction(client)
    # no / bad token
    assert client.post(f"/api/rooms/{a.code}/bid", json={"amount": 100}).status_code == 401
    r = client.post(f"/api/rooms/{a.code}/bid", headers={"Authorization": "Bearer nope"}, json={"amount": 100})
    assert r.status_code == 401
    # a body franchise_id that is not the caller's franchise is refused, not trusted
    other = next(f["id"] for f in a.snap()["franchises"] if f["name"] == "CSK")
    r = a.bid("MI", 100, franchise_id=other)
    assert r.status_code == 403 and r.json()["code"] == "FRANCHISE_NOT_AUTHORISED"
    # host with no franchise cannot bid
    r = a.bid("HOST", 100)
    assert r.status_code == 403 and r.json()["code"] == "FRANCHISE_NOT_AUTHORISED"
    assert a.count(Bid) == 0
    # unknown room
    r = client.post("/api/rooms/ZZZZZZ/bid", headers=a.H("MI"), json={"amount": 100})
    assert r.status_code == 404
    # a token from another room is not valid here
    b = make_auction(client)
    r = client.post(f"/api/rooms/{a.code}/bid", headers=b.H("MI"), json={"amount": 100})
    assert r.status_code == 401


def test_host_bids_through_selected_franchise_only(client):
    a = make_auction(client, host_franchise="RCB")
    r = a.bid("HOST", 100)
    assert r.status_code == 200 and r.json()["leading_franchise_name"] == "RCB"
    assert r.json()["your_franchise_id"] == r.json()["leading_franchise_id"]


def test_bid_is_pinned_to_the_player_the_bidder_saw(client):
    a = make_auction(client)
    cp = a.current()
    r = a.bid("MI", 100, lot_id=cp["lot_id"] + 999)
    assert r.status_code == 409 and r.json()["code"] == "PLAYER_ALREADY_SETTLED"
    r = a.bid("MI", 100, lot_id=cp["lot_id"], player_id=cp["player_id"] + 999)
    assert r.status_code == 409 and r.json()["code"] == "PLAYER_ALREADY_SETTLED"
    assert a.count(Bid) == 0


def test_bid_when_auction_not_live(client):
    a = make_auction(client, start=False)
    r = client.post(f"/api/rooms/{a.code}/bid", headers=a.H("MI"), json={"amount": 100})
    assert r.status_code == 409 and r.json()["code"] == "AUCTION_NOT_LIVE"
    a2 = make_auction(client)
    cp = a2.current()
    assert a2.host("pause").status_code == 200
    r = a2.bid("MI", 100, lot_id=cp["lot_id"])
    assert r.status_code == 409 and r.json()["code"] == "AUCTION_NOT_LIVE"


# ---------------------------------------------------------------- Test D
def test_d_highest_bidder_cannot_bid_against_itself(client):
    a = make_auction(client)
    assert a.bid("MI", 100).status_code == 200
    for amount in (110, 100):
        r = a.bid("MI", amount)
        assert r.status_code == 409 and r.json()["code"] == "ALREADY_HIGHEST_BIDDER"
    assert a.bid("CSK", 110).status_code == 200      # someone else takes the lead...
    assert a.bid("MI", 120).status_code == 200       # ...so MI may bid again
    assert a.count(Bid) == 3


# ---------------------------------------------------------------- Test E
def test_e_insufficient_purse(client):
    a = make_auction(client, purse=100, min_squad=1)
    assert a.bid("MI", 100).status_code == 200          # MI spends its whole purse on the lead
    r = a.bid("CSK", 110)                               # CSK has only 100
    assert r.status_code == 409 and r.json()["code"] == "INSUFFICIENT_PURSE"
    assert a.db_lot()[1].current_bid == 100


def test_e_purse_must_still_cover_minimum_squad(client):
    a = make_auction(client, purse=100, min_squad=2)    # after this player 1 more is needed (reserve 10)
    r = a.bid("MI", 100)
    assert r.status_code == 409 and r.json()["code"] == "INSUFFICIENT_PURSE"
    assert "minimum squad" in r.json()["detail"]


# ---------------------------------------------------------------- Test C
def _race(a, bidders, amount, rounds_barrier=True):
    results = {}
    barrier = threading.Barrier(len(bidders))
    lot_id = a.current()["lot_id"]

    def go(name):
        barrier.wait()
        results[name] = a.bid(name, amount, lot_id=lot_id)

    ts = [threading.Thread(target=go, args=(n,)) for n in bidders]
    [t.start() for t in ts]; [t.join(30) for t in ts]
    return results


def test_c_near_simultaneous_bids_are_serialised(client):
    a = make_auction(client)
    with slow_commits(0.25):                       # widens the commit window so the requests truly overlap
        res = _race(a, ["MI", "CSK"], 100)
    codes = sorted(r.status_code for r in res.values())
    assert codes == [200, 409], {k: (v.status_code, v.text) for k, v in res.items()}
    winner = next(k for k, v in res.items() if v.status_code == 200)
    loser = next(k for k, v in res.items() if v.status_code == 409)
    assert res[loser].json()["code"] == "STALE_BID"
    assert res[loser].json()["state"]["next_bid_amount"] == 110
    assert res[loser].json()["state"]["leading_franchise_name"] == winner
    room, lot = a.db_lot()
    assert lot.current_bid == 100 and lot.bid_sequence == 1 and a.count(Bid) == 1
    assert a.purse(loser) == 5000


def test_c_three_way_race_has_exactly_one_winner_at_the_old_price(client):
    a = make_auction(client)
    with slow_commits(0.1):
        res = _race(a, ["MI", "CSK", "RCB"], 100)
    assert sorted(r.status_code for r in res.values()) == [200, 409, 409]
    assert a.count(Bid) == 1 and a.db_lot()[1].current_bid == 100


def test_c_versions_are_strictly_sequential_under_load(client):
    a = make_auction(client)
    v = a.snap()["room"]["version"]
    names = ["MI", "CSK", "RCB"]
    seen = []
    for i in range(9):
        r = a.bid(names[i % 3])
        assert r.status_code == 200
        seen.append(r.json()["room_version"])
    assert seen == list(range(v + 1, v + 10))
    assert a.snap()["room"]["version"] == v + 9
    assert a.count(Bid) == 9


# ---------------------------------------------------------------- Test F
def test_f_bid_after_deadline_is_rejected_and_nothing_changes(client):
    a = make_auction(client)
    lot_id = a.current()["lot_id"]
    a.set_deadline(-1)                              # expired, supervisor has not settled it yet
    r = a.bid("MI", 100, lot_id=lot_id)
    assert r.status_code == 409 and r.json()["code"] == "TIMER_EXPIRED"
    assert a.count(Bid) == 0 and a.purse("MI") == 5000


def test_f_no_bid_is_accepted_after_the_player_was_settled(client):
    a = make_auction(client)
    old = a.current()
    assert a.bid("MI", 100).status_code == 200
    a.set_deadline(-1)
    settled, nxt, _hold = main._settle_expired_lot(a.code)
    assert settled and nxt is not None
    assert a.count(Sale) == 1 and a.purse("MI") == 4900
    # a late bid for the player who was just sold must fail and must not touch anything
    r = a.bid("CSK", 110, lot_id=old["lot_id"])
    assert r.status_code == 409 and r.json()["code"] == "PLAYER_ALREADY_SETTLED"
    assert a.count(Sale) == 1 and a.count(Bid) == 1 and a.purse("CSK") == 5000


def test_f_settlement_waits_for_an_in_flight_bid_and_then_sees_the_extended_timer(client):
    a = make_auction(client, timer=25)
    lot_id = a.current()["lot_id"]
    a.set_deadline(0.5)
    out = {}

    def bidder():
        out["bid"] = a.bid("MI", 100, lot_id=lot_id)

    def settler():
        time.sleep(0.7)                             # original deadline has passed by now...
        out["settle"] = main._settle_expired_lot(a.code)

    with slow_commits(1.0):                         # ...but the bid is still committing
        tb, ts = threading.Thread(target=bidder), threading.Thread(target=settler)
        tb.start(); time.sleep(0.05); ts.start(); tb.join(30); ts.join(30)
    assert out["bid"].status_code == 200, out["bid"].text
    settled, deadline, _hold = out["settle"]
    assert settled is False and deadline is not None        # timer was extended by the committed bid
    room, lot = a.db_lot()
    assert lot.status == "LIVE" and lot.current_bid == 100
    assert a.count(Sale) == 0 and a.purse("MI") == 5000     # not sold underneath the bid


def test_f_expired_rejection_rearms_the_timer_so_the_player_still_settles(client):
    a = make_auction(client)
    old = a.current()
    assert a.bid("MI", 100).status_code == 200
    a.set_deadline(-1)
    r = a.bid("CSK", 110, lot_id=old["lot_id"])
    assert r.status_code == 409 and r.json()["code"] == "TIMER_EXPIRED"
    deadline = time.time() + 5
    while time.time() < deadline and a.count(Sale) == 0:    # supervisor settles immediately
        time.sleep(0.1)
    assert a.count(Sale) == 1 and a.purse("MI") == 4900


# ---------------------------------------------------------------- Test J
def test_j_failure_before_commit_is_not_acknowledged_or_broadcast(client, monkeypatch):
    a = make_auction(client)
    probe = WSProbe(client, a.code, a.tokens["CSK"])
    try:
        probe.until("snapshot")
        monkeypatch.setattr(main, "apply_bid", lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("write failed")))
        r = a.bid("MI", 100)
        assert r.status_code == 503 and r.json()["code"] == "BID_NOT_SAVED" and r.json()["accepted"] is False
        monkeypatch.undo()
        assert a.count(Bid) == 0 and a.db_lot()[1].current_bid == 0
        assert [m for m in probe.drain() if m["type"] == "BID_ACCEPTED"] == []
        assert a.bid("MI", 100).status_code == 200           # previous committed state stayed authoritative
        assert probe.until("BID_ACCEPTED")["highest_bid"] == 100
    finally:
        probe.close()


def test_j_commit_failure_is_not_acknowledged_or_broadcast(client, monkeypatch):
    from sqlalchemy.orm import Session
    a = make_auction(client)
    probe = WSProbe(client, a.code, a.tokens["CSK"])
    try:
        probe.until("snapshot")
        real_commit = Session.commit
        state = {"fail": True}

        def flaky_commit(self):
            if state["fail"]:
                state["fail"] = False
                raise OperationalError("COMMIT", {}, Exception("connection lost"))
            return real_commit(self)
        monkeypatch.setattr(Session, "commit", flaky_commit)
        r = a.bid("MI", 100)
        assert r.status_code == 503 and r.json()["code"] == "BID_NOT_CONFIRMED" and r.json()["accepted"] is False
        monkeypatch.undo()
        assert a.count(Bid) == 0 and a.db_lot()[1].current_bid == 0 and a.purse("MI") == 5000
        assert [m for m in probe.drain() if m["type"] == "BID_ACCEPTED"] == []
        # the same bid, retried deliberately, is accepted at the same price
        assert a.bid("MI", 100).status_code == 200
    finally:
        probe.close()


# ---------------------------------------------------------------- misc performance guarantees
def test_successful_bid_never_builds_a_full_snapshot(client, monkeypatch):
    a = make_auction(client)
    lot_id = a.current()["lot_id"]

    def boom(*args, **kwargs):
        raise AssertionError("snapshot() must not run on the bid path")
    monkeypatch.setattr(main, "snapshot", boom)
    monkeypatch.setattr(engine_mod, "snapshot", boom)
    assert a.bid("MI", 100, lot_id=lot_id).status_code == 200
    assert a.bid("CSK", 110, lot_id=lot_id).status_code == 200
    assert a.bid("CSK", 120, lot_id=lot_id).status_code == 409      # rejection path as well


def test_bid_timing_log_is_structured_and_leaks_no_credentials(client, caplog):
    import logging
    a = make_auction(client)
    caplog.set_level(logging.INFO, logger="smat-auction")
    assert a.bid("MI", 100).status_code == 200
    assert a.bid("CSK", 100).status_code == 409
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("bid_timing")]
    assert len(lines) == 2
    ok, bad = lines
    for key in ("request_id=", "room=" + a.code, "submitted=100", "outcome=accepted", "accepted_amount=100",
                "version=", "auth_ms=", "lock_wait_ms=", "read_auth_ms=", "validate_ms=", "write_ms=",
                "commit_ms=", "response_ms=", "broadcast_enqueue_ms=", "total_ms=", "db_stmts="):
        assert key in ok, (key, ok)
    assert "outcome=rejected" in bad and "reason=STALE_BID" in bad and "submitted=100" in bad
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in list(a.tokens.values()) + [a.host_token]:
        assert secret not in blob


def test_request_id_header_is_echoed_when_valid(client):
    a = make_auction(client)
    cp = a.current()
    r = client.post(f"/api/rooms/{a.code}/bid", headers={**a.H("MI"), "X-Request-Id": "abc123-XYZ"},
                    json={"amount": 100, "lot_id": cp["lot_id"]})
    assert r.json()["request_id"] == "abc123-XYZ"
    r = client.post(f"/api/rooms/{a.code}/bid", headers={**a.H("CSK"), "X-Request-Id": "bad id with spaces!"},
                    json={"amount": 110, "lot_id": cp["lot_id"]})
    assert r.json()["request_id"] != "bad id with spaces!" and len(r.json()["request_id"]) > 5


def test_undo_then_new_bid_discards_redo_branch(client):
    a = make_auction(client)
    assert a.bid("MI", 100).status_code == 200
    assert a.bid("CSK", 110).status_code == 200
    assert a.host("undo-bid").status_code == 200
    s = a.snap()
    assert s["current_player"]["current_bid"] == 100 and s["history"]["can_redo_bid"] is True
    # after the undo the price is 110 again; a client still showing 120 is stale
    assert a.bid("RCB", 120).status_code == 409
    assert a.bid("RCB", 110).status_code == 200
    s = a.snap()
    assert s["history"]["can_redo_bid"] is False and s["history"]["can_undo_bid"] is True
    assert s["current_player"]["highest_franchise"] == "RCB"
    assert a.host("redo-bid").status_code == 400            # redo branch was discarded


@pytest.mark.skipif(not PG, reason="needs PostgreSQL (row-level re-check of a guarded UPDATE)")
def test_pg_database_guard_stops_double_accept_even_without_the_process_lock(client, monkeypatch):
    """Defence in depth: if two *processes* (e.g. overlapping Render deploys) raced, the guarded
    UPDATE still lets only one bid win. We simulate that by disabling the in-process lock."""
    import contextlib
    a = make_auction(client)
    monkeypatch.setattr(main, "lock_for", lambda key: contextlib.nullcontext())
    with slow_commits(0.2):
        res = _race(a, ["MI", "CSK", "RCB"], 100)
    assert sorted(r.status_code for r in res.values()) == [200, 409, 409], {k: (v.status_code, v.text) for k, v in res.items()}
    assert a.count(Bid) == 1 and a.db_lot()[1].bid_sequence == 1


def test_index_assets_are_content_stamped_and_never_cached(client):
    """Regression for the live-test failure: browsers kept an old app.js after a deploy."""
    import re
    r = client.get("/")
    assert r.headers["cache-control"].startswith("no-store")
    refs = re.findall(r'/static/(?:app\.js|style\.css)\?v=([0-9a-f]{10})', r.text)
    assert len(refs) == 2
    js = client.get(re.search(r'/static/app\.js\?v=[0-9a-f]{10}', r.text).group(0))
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert "BID_ACCEPTED" in js.text          # the page really is the new frontend
