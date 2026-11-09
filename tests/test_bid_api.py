import os
import tempfile
from pathlib import Path

# The app engine is created at import time. Keep API tests isolated from any local
# or deployed database that may be configured in the user's environment.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(Path(tempfile.mkdtemp()) / "api-test.db"))

from fastapi.testclient import TestClient
from app.main import app


def create_live_room(client):
    created = client.post("/api/rooms", json={
        "name": "Bid semantics test", "franchises": ["MI", "CSK"],
        "purse": 5000, "timer_seconds": 25, "min_squad": 1, "max_squad": 5,
        "players": [{"name": "Test Batter", "state_team": "Mumbai", "role": "BAT", "base_price": 100}],
    })
    assert created.status_code == 200, created.text
    data = created.json()
    tokens = {x["franchise"]: x["token"] for x in data["franchise_invites"]}
    host = {"Authorization": "Bearer " + data["host_token"]}
    mi = {"Authorization": "Bearer " + tokens["MI"]}
    csk = {"Authorization": "Bearer " + tokens["CSK"]}
    started = client.post(f"/api/rooms/{data['room_code']}/start", headers=host)
    assert started.status_code == 200, started.text
    return data["room_code"], mi, csk


def test_stale_bid_is_rejected_without_auto_escalation_and_returns_latest_price():
    with TestClient(app) as client:
        code, mi, csk = create_live_room(client)
        first = client.post(f"/api/rooms/{code}/bid", headers=mi, json={"amount": 100})
        assert first.status_code == 200, first.text
        assert first.json()["highest_bid"] == 100
        assert first.json()["next_bid_amount"] == 110

        stale = client.post(f"/api/rooms/{code}/bid", headers=csk, json={"amount": 100})
        assert stale.status_code == 409, stale.text
        detail = stale.json()["detail"]
        assert detail["code"] == "STALE_BID"
        assert detail["current_bid"] == 100
        assert detail["leading_franchise"] == "MI"
        assert detail["next_bid"] == 110

        # A new, conscious click at the newly displayed amount is accepted.
        second = client.post(f"/api/rooms/{code}/bid", headers=csk, json={"amount": 110})
        assert second.status_code == 200, second.text
        assert second.json()["highest_bid"] == 110
        assert second.json()["leading_franchise_name"] == "CSK"


def test_same_franchise_cannot_bid_twice_in_a_row():
    with TestClient(app) as client:
        code, mi, _csk = create_live_room(client)
        assert client.post(f"/api/rooms/{code}/bid", headers=mi, json={"amount": 100}).status_code == 200
        rejected = client.post(f"/api/rooms/{code}/bid", headers=mi, json={"amount": 110})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "ALREADY_HIGHEST_BIDDER"


def test_concurrent_same_price_bids_are_serialized():
    from concurrent.futures import ThreadPoolExecutor
    with TestClient(app) as client:
        code, mi, csk = create_live_room(client)
        def submit(headers):
            return client.post(f"/api/rooms/{code}/bid", headers=headers, json={"amount": 100})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (mi, csk)))
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 409], [(r.status_code, r.text) for r in results]
        rejected = next(r for r in results if r.status_code == 409).json()["detail"]
        assert rejected["code"] == "STALE_BID"
        assert rejected["next_bid"] == 110
