"""Important players get a longer clock, only in rooms with a 25 s timer or more."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from app.engine import seconds_until
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _weighted(client, timer, weights):
    payload = {"name": "T", "franchises": ["A", "B"], "purse": 5000, "timer_seconds": timer, "min_squad": 1,
               "max_squad": 25, "bid_extension": True,
               "players": [{"name": f"P{i}", "state_team": "X", "role": "BAT", "base_price": 100,
                            "importance_weight": w} for i, w in enumerate(weights)]}
    r = client.post("/api/rooms", json=payload); assert r.status_code == 200, r.text
    from helpers import Auction
    a = Auction(client, r.json(), ["A", "B"])
    assert client.post(f"/api/rooms/{a.code}/start", headers=a.H("HOST")).status_code == 200
    return a


def _clock_for_each_player(a, n):
    seen = {}
    for _ in range(n):
        cur = a.current()
        _, lot = a.db_lot()
        seen[cur["name"]] = (cur["timer_seconds"], round(seconds_until(lot.ends_at)))
        a.client.post(f"/api/rooms/{a.code}/host/advance", headers=a.H("HOST"))
    return seen


def test_clock_scales_with_importance_in_25s_rooms(client):
    a = _weighted(client, 25, [1.0, 4.0, 16.0])      # 16 is the pool's top weight
    seen = _clock_for_each_player(a, 3)
    assert seen["P0"] == (25, 25)                      # ordinary player: unchanged
    assert seen["P2"] == (38, 38)                      # top weight: +half the timer (12.5, rounded to 13)
    assert 25 < seen["P1"][0] < 38                     # log scale: weight 4 is halfway to 16 -> about +6
    assert seen["P1"][0] == 31


def test_shorter_timers_are_never_changed(client):
    for timer in (5, 10):
        a = _weighted(client, timer, [1.0, 16.0])
        assert set(v for v, _ in _clock_for_each_player(a, 2).values()) == {timer}


def test_all_default_weights_change_nothing(client):
    a = _weighted(client, 25, [1.0, 1.0, 1.0])
    assert set(v for v, _ in _clock_for_each_player(a, 3).values()) == {25}


def test_extension_pause_and_all_out_use_the_players_own_window(client):
    a = _weighted(client, 25, [16.0, 16.0])          # both players are top weight: 38 s clock
    assert a.current()["timer_seconds"] == 38
    a.set_deadline(5)
    assert a.bid("A", 100).status_code == 200
    _, lot = a.db_lot()
    assert 23.5 <= seconds_until(lot.ends_at) <= 24.2   # 5 + half of 38, capped at 38
    assert a.host("pause").status_code == 200
    assert a.current()["remaining_ms"] >= 19000          # pause floor is half of THIS window, not half of 25
    assert a.host("resume").status_code == 200
    r = a.client.post(f"/api/rooms/{a.code}/pass", headers=a.H("B"), json={"lot_id": a.current()["lot_id"], "out": True})
    assert r.status_code == 200
    _, lot = a.db_lot()
    assert seconds_until(lot.ends_at) <= 19.1            # all out: trimmed to half of 38
