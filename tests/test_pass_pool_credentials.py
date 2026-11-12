"""'I'm out' flag, auction pool view, recoverable passwords."""
import time
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.models import Sale
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _pass(a, who, out=True, lot_id=None):
    lot_id = lot_id or a.current()["lot_id"]
    return a.client.post(f"/api/rooms/{a.code}/pass", headers=a.H(who), json={"lot_id": lot_id, "out": out})


def _names(a):
    return [f["name"] for f in a.snap()["franchises"]]


def test_out_is_visible_in_snapshot_and_reversible(client):
    a = make_auction(client, base=100)
    n = _names(a)
    assert _pass(a, n[1]).status_code == 200
    fid = {f["name"]: f["id"] for f in a.snap()["franchises"]}
    assert a.current()["out_ids"] == [fid[n[1]]]
    assert _pass(a, n[1], out=False).status_code == 200
    assert a.current()["out_ids"] == []


def test_leader_cannot_step_out_and_bidding_again_voids_the_pass(client):
    a = make_auction(client, base=100)
    n = _names(a)
    a.bid(n[0], 100)
    r = _pass(a, n[0]); assert r.status_code == 409
    assert _pass(a, n[1]).status_code == 200
    time.sleep(0.02)
    assert a.bid(n[1]).status_code == 200          # bids anyway: no longer out
    fid = {f["name"]: f["id"] for f in a.snap()["franchises"]}
    assert fid[n[1]] not in a.current()["out_ids"]


def test_everyone_out_with_one_leader_shortens_the_clock_then_sells(client):
    a = make_auction(client, base=100, timer=25)
    n = _names(a)
    a.bid(n[0], 100)
    for who in n[1:]:
        assert _pass(a, who).status_code == 200
    _, lot = a.db_lot()
    from app.engine import seconds_until
    assert seconds_until(lot.ends_at) <= 1.6
    a.set_deadline(-1.5)
    settled, _, _ = main._settle_expired_lot(a.code)
    assert settled and a.count(Sale, reversed=False) == 1


def test_nobody_bid_and_everyone_out_goes_unsold(client):
    a = make_auction(client, base=100, timer=25)
    for who in _names(a):
        assert _pass(a, who).status_code == 200
    _, lot = a.db_lot()
    from app.engine import seconds_until
    assert seconds_until(lot.ends_at) <= 1.6
    a.set_deadline(-1.5)
    main._settle_expired_lot(a.code)
    assert a.count(Sale, reversed=False) == 0
    assert a.snap()["unsold"]


def test_someone_returning_restores_a_fair_clock(client):
    a = make_auction(client, base=100, timer=10)
    n = _names(a)
    a.bid(n[0], 100)
    for who in n[1:]:
        _pass(a, who)
    assert _pass(a, n[1], out=False).status_code == 200
    _, lot = a.db_lot()
    from app.engine import seconds_until
    assert seconds_until(lot.ends_at) > 3.5


def test_pass_for_a_stale_lot_is_rejected(client):
    a = make_auction(client, base=100)
    n = _names(a)
    assert _pass(a, n[0], lot_id=999999).status_code == 409


def test_host_without_franchise_cannot_pass(client):
    a = make_auction(client, base=100)
    assert _pass(a, "HOST").status_code == 403


def test_pool_lists_upcoming_sold_and_unsold(client):
    a = make_auction(client, players=4, base=100)
    n = _names(a)
    a.bid(n[0], 100); a.set_deadline(-1); main._settle_expired_lot(a.code)     # sold
    a.set_deadline(-1); main._settle_expired_lot(a.code)                        # unsold
    r = client.get(f"/api/rooms/{a.code}/pool", headers=a.H(n[1])).json()
    c = r["counts"]
    assert c["SOLD"] == 1 and c["UNSOLD"] == 1 and c["LIVE"] + c["UPCOMING"] == 2
    sold = [p for p in r["players"] if p["status"] == "SOLD"][0]
    assert sold["price"] == 100 and sold["franchise"] == n[0]


def test_credentials_can_be_viewed_again_and_survive_reset(client):
    d = client.post("/api/rooms", json={"name": "T", "franchises": ["A1", "B2"], "players": [
        {"name": "P1", "state_team": "X", "role": "BAT", "base_price": 20}]}).json()
    host = {"Authorization": "Bearer " + d["host_token"]}
    c = client.get(f"/api/rooms/{d['room_code']}/host/credentials", headers=host).json()
    given = {x["franchise"]: x["password"] for x in d["credentials"]}
    assert {f["name"]: f["password"] for f in c["franchises"]} == given
    assert c["host"]["password"] == d["host_login"]["password"]
    fid = c["franchises"][0]["id"]
    new = client.post(f"/api/rooms/{d['room_code']}/host/reset-password", json={"franchise_id": fid}, headers=host).json()
    c2 = client.get(f"/api/rooms/{d['room_code']}/host/credentials", headers=host).json()
    assert c2["franchises"][0]["password"] == new["password"] != c["franchises"][0]["password"]
    # a franchise cannot read them
    tok = d["franchise_invites"][1]["token"]
    assert client.get(f"/api/rooms/{d['room_code']}/host/credentials", headers={"Authorization": "Bearer " + tok}).status_code == 403
