"""Host: pick the next player (from the pool or brand new), plus the half-timer rule for 'everyone out'."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.engine import seconds_until
from app.models import Lot, Player, Sale
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _q(a, who="HOST", **body):
    return a.client.post(f"/api/rooms/{a.code}/queue-player", headers=a.H(who), json=body)


def _pool(a):
    return a.client.get(f"/api/rooms/{a.code}/auction-pool", headers=a.H("HOST")).json()["players"]


def _settle_current(a):
    a.set_deadline(-2)
    main._settle_expired_lot(a.code)
    main._open_after_hold(a.code)


def test_host_setup_pool_endpoint_is_still_the_plain_player_list(client):
    a = make_auction(client, start=False)
    rows = client.get(f"/api/rooms/{a.code}/pool", headers=a.H("HOST")).json()
    assert isinstance(rows, list) and rows[0]["included"] is True and "importance_weight" in rows[0]


def test_pool_player_jumps_the_queue_and_is_not_duplicated(client):
    a = make_auction(client, players=6)
    last = [p for p in _pool(a) if p["status"] == "UPCOMING"][-1]
    r = _q(a, player_id=last["id"])
    assert r.status_code == 200, r.text
    assert r.json()["next_up"]["player_id"] == last["id"]
    assert a.count(Lot, player_id=last["id"]) == 1          # moved, not duplicated
    _settle_current(a)
    assert a.current()["player_id"] == last["id"]


def test_new_player_can_be_added_mid_auction_next_or_at_the_end(client):
    a = make_auction(client, players=3)
    r = _q(a, new={"name": "Fresh Face", "state_team": "Goa", "role": "bat", "base_price": 50})
    assert r.status_code == 200, r.text
    assert r.json()["next_up"]["name"] == "Fresh Face" and r.json()["next_up"]["base_price"] == 50
    assert a.count(Player, name="Fresh Face") == 1
    assert _q(a, new={"name": "Fresh Face", "state_team": "Goa"}).status_code == 400   # duplicate
    r = _q(a, new={"name": "Last Man"}, position="end")
    assert r.status_code == 200
    assert r.json()["next_up"]["name"] == "Fresh Face"
    _settle_current(a)
    assert a.current()["name"] == "Fresh Face"


def test_sold_or_on_block_player_cannot_be_queued_and_only_host_can_queue(client):
    a = make_auction(client, players=3)
    n = a.names
    cur = a.current()
    assert _q(a, player_id=cur["player_id"]).status_code == 400           # on the block
    assert _q(a, who=n[0], player_id=cur["player_id"]).status_code == 403
    a.bid(n[0])
    _settle_current(a)
    assert _q(a, player_id=cur["player_id"]).status_code == 400           # sold


def test_unsold_player_can_be_brought_straight_back(client):
    a = make_auction(client, players=3)
    first = a.current()["player_id"]
    _settle_current(a)                                                    # nobody bid: unsold
    assert _q(a, player_id=first, base_price=30).status_code == 200
    _settle_current(a)
    cur = a.current()
    assert cur["player_id"] == first and cur["base_price"] == 30


def test_empty_queue_opens_the_chosen_player_immediately(client):
    a = make_auction(client, players=1)
    _settle_current(a)
    assert a.snap()["room"]["status"] == "ROUND_COMPLETE"
    r = _q(a, new={"name": "Late Entry", "base_price": 40})
    assert r.status_code == 200 and r.json()["room"]["status"] == "LIVE"
    assert a.current()["name"] == "Late Entry"


def test_everyone_out_keeps_half_the_timer_but_never_lengthens_it(client):
    a = make_auction(client, base=100, timer=25)
    n = a.names
    a.bid(n[0], 100)
    for who in n[1:]:
        assert a.client.post(f"/api/rooms/{a.code}/pass", headers=a.H(who),
                             json={"lot_id": a.current()["lot_id"], "out": True}).status_code == 200
    _, lot = a.db_lot()
    assert 11.0 <= seconds_until(lot.ends_at) <= 12.6
    # when less than half is already left, the clock is left alone
    b = make_auction(client, base=100, timer=25)
    b.bid(b.names[0], 100)
    b.set_deadline(4)
    for who in b.names[1:]:
        b.client.post(f"/api/rooms/{b.code}/pass", headers=b.H(who), json={"lot_id": b.current()["lot_id"], "out": True})
    _, lot = b.db_lot()
    assert seconds_until(lot.ends_at) <= 4.1
