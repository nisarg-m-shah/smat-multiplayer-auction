"""WebSocket fan-out, ordering, reconnection and slow-client isolation (Tests G, I)."""
import asyncio, time
import pytest
from fastapi.testclient import TestClient

import app.main as main
from helpers import make_auction, WSProbe


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def test_g_three_clients_receive_the_same_compact_update(client):
    a = make_auction(client)
    probes = {n: WSProbe(client, a.code, a.tokens[n]) for n in ("MI", "CSK", "RCB")}
    try:
        for p in probes.values():
            assert p.until("snapshot")["reason"] == "connect"
        r = a.bid("MI", 100)                                   # bidder is acknowledged over HTTP
        assert r.status_code == 200 and r.json()["highest_bid"] == 100
        events = {n: p.until("BID_ACCEPTED") for n, p in probes.items()}
        for e in events.values():                              # everybody (incl. bidder) sees identical state
            assert (e["highest_bid"], e["leading_franchise_name"], e["next_bid_amount"], e["room_version"]) == \
                   (100, "MI", 110, r.json()["room_version"])
            assert "franchises" not in e and len(str(e)) < 800
        # no full snapshot follows a normal bid
        for p in probes.values():
            assert [m for m in p.drain() if m["type"] == "snapshot"] == []
        # a second bid keeps all three in step, in order
        assert a.bid("CSK", 110).status_code == 200
        for p in probes.values():
            e = p.until("BID_ACCEPTED")
            assert e["leading_franchise_name"] == "CSK" and e["room_version"] == r.json()["room_version"] + 1
    finally:
        for p in probes.values():
            p.close()


def test_events_arrive_in_commit_order_under_rapid_bidding(client):
    a = make_auction(client)
    p = WSProbe(client, a.code, a.tokens["RCB"])
    try:
        p.until("snapshot")
        names = ["MI", "CSK"]
        versions = [a.bid(names[i % 2]).json()["room_version"] for i in range(10)]
        got = [p.until("BID_ACCEPTED")["room_version"] for _ in range(10)]
        assert got == versions == sorted(versions)
    finally:
        p.close()


def test_settlement_is_published_as_a_full_snapshot_with_a_new_version(client):
    a = make_auction(client)
    p = WSProbe(client, a.code, a.tokens["MI"])
    try:
        p.until("snapshot")
        r = a.bid("MI", 100).json()
        p.until("BID_ACCEPTED")
        a.set_deadline(-1)
        main._settle_expired_lot(a.code)
        snap = p.until("snapshot")["data"]
        assert snap["room"]["version"] == r["room_version"] + 1
        assert snap["current_player"]["lot_id"] != r["lot_id"]
        assert any(e["kind"] == "SOLD" for e in snap["events"])
    finally:
        p.close()


def test_i_reconnecting_client_recovers_latest_state_from_a_snapshot(client):
    a = make_auction(client)
    p1 = WSProbe(client, a.code, a.tokens["MI"])
    p2 = WSProbe(client, a.code, a.tokens["CSK"])
    try:
        p1.until("snapshot"); p2.until("snapshot")
        assert a.bid("MI", 100).status_code == 200
        p2.until("BID_ACCEPTED")
        p2.close()                                             # CSK drops (e.g. Render interrupts the socket)
        assert a.bid("RCB", 110).status_code == 200            # the room carries on without it
        assert a.bid("MI", 120).status_code == 200
        v = a.snap()["room"]["version"]
        p2 = WSProbe(client, a.code, a.tokens["CSK"])          # CSK reconnects
        snap = p2.until("snapshot")
        assert snap["reason"] == "connect"
        cp = snap["data"]["current_player"]
        assert (cp["current_bid"], cp["highest_franchise"], snap["data"]["room"]["version"]) == (120, "MI", v)
    finally:
        p1.close(); p2.close()


def test_a_joining_client_does_not_trigger_snapshots_for_everyone_else(client):
    a = make_auction(client)
    p1 = WSProbe(client, a.code, a.tokens["MI"])
    try:
        p1.until("snapshot")
        p2 = WSProbe(client, a.code, a.tokens["CSK"])
        p2.until("snapshot")
        assert p1.drain(0.6) == []
        p2.close()
    finally:
        p1.close()


def test_websocket_rejects_bad_token(client):
    a = make_auction(client)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{a.code}?token=wrong") as ws:
            ws.receive_json()


def test_ping_pong_heartbeat_still_works(client):
    a = make_auction(client)
    p = WSProbe(client, a.code, a.tokens["MI"])
    try:
        p.until("snapshot")
        p.ws.send_text("ping")
        assert p.until("pong")
    finally:
        p.close()


# ---------- ConnectionManager unit tests (no network): slow / dead clients must not block others
class FakeWS:
    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail, self.sent, self.closed = delay, fail, [], False

    async def accept(self): pass

    async def send_text(self, t):
        if self.fail:
            raise RuntimeError("connection reset")
        await asyncio.sleep(self.delay)
        self.sent.append(t)

    async def close(self, code=1000): self.closed = True


def test_a_hung_client_cannot_delay_or_block_other_clients(monkeypatch):
    monkeypatch.setattr(main, "SEND_TIMEOUT", 0.3)

    async def scenario():
        m = main.ConnectionManager()
        hung, dead, fast1, fast2 = FakeWS(delay=60), FakeWS(fail=True), FakeWS(), FakeWS()
        for w in (hung, dead, fast1, fast2):
            await m.connect("ROOM1", w)
        t0 = time.perf_counter()
        for v in range(1, 6):
            m.broadcast_nowait("ROOM1", {"type": "BID_ACCEPTED", "room_version": v}, time.perf_counter())
        enqueue_ms = (time.perf_counter() - t0) * 1000
        await asyncio.sleep(0.15)
        assert enqueue_ms < 50                                  # broadcasting never waits on any client
        for w in (fast1, fast2):                                # healthy clients got all five, in order
            import json
            assert [json.loads(x)["room_version"] for x in w.sent] == [1, 2, 3, 4, 5]
        await asyncio.sleep(0.6)                                # hung client times out, dead one dropped
        assert dead.closed and hung.closed
        assert len(m.connections["ROOM1"]) == 2

    asyncio.run(scenario())


def test_a_client_that_falls_far_behind_is_dropped_not_buffered_forever(monkeypatch):
    monkeypatch.setattr(main, "SEND_QUEUE_MAX", 3)

    async def scenario():
        m = main.ConnectionManager()
        slow, fast = FakeWS(delay=5), FakeWS()
        await m.connect("ROOM2", slow); await m.connect("ROOM2", fast)
        for v in range(10):
            m.broadcast_nowait("ROOM2", {"type": "BID_ACCEPTED", "room_version": v})
            await asyncio.sleep(0.01)          # real traffic: the event loop runs between messages
        await asyncio.sleep(0.2)
        assert len(fast.sent) == 10 and slow.closed
        assert len(m.connections["ROOM2"]) == 1

    asyncio.run(scenario())
