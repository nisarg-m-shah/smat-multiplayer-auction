"""Franchise name + password login: same tokens as the invite links, additive sessions, host resets."""
import pytest
from fastapi.testclient import TestClient
import app.main as main
from helpers import make_auction


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _create(client, names=("Mumbai", "Chennai")):
    r = client.post("/api/rooms", json={"name": "T", "franchises": list(names), "players": [
        {"name": "P1", "state_team": "X", "role": "BAT", "base_price": 20},
        {"name": "P2", "state_team": "X", "role": "BOWL", "base_price": 20}]})
    assert r.status_code == 200, r.text
    return r.json()


def _login(client, room, who, pw):
    return client.post("/api/login", json={"room": room, "franchise": who, "password": pw})


def test_create_returns_credentials_and_keeps_links(client):
    d = _create(client)
    assert {c["franchise"] for c in d["credentials"]} == {"Mumbai", "Chennai"}
    assert d["host_login"]["franchise"] == "Host" and d["host_login"]["password"]
    assert len(d["franchise_invites"]) == 2


def test_login_issues_the_same_token_as_the_link_on_every_device(client):
    d = _create(client)
    pw = {c["franchise"]: c["password"] for c in d["credentials"]}
    link = {x["franchise"]: x["token"] for x in d["franchise_invites"]}
    a = _login(client, d["room_code"], "Mumbai", pw["Mumbai"]).json()
    b = _login(client, d["room_code"].lower(), " mumbai ", pw["Mumbai"].lower().replace("-", "")).json()
    assert a["token"] == b["token"] == link["Mumbai"]       # a second device never signs the first out
    assert a["franchise"]["name"] == "Mumbai" and not a["is_host"]
    me = client.get(f"/api/rooms/{d['room_code']}/me", headers={"Authorization": "Bearer " + a["token"]})
    assert me.status_code == 200 and me.json()["franchise"]["name"] == "Mumbai"


def test_host_login(client):
    d = _create(client)
    r = _login(client, d["room_code"], "host", d["host_login"]["password"]).json()
    assert r["is_host"] and r["token"] == d["host_token"]


def test_wrong_credentials_are_a_generic_401(client):
    d = _create(client)
    pw = d["credentials"][0]["password"]
    for room, who, p in [(d["room_code"], "Mumbai", "WRONG-PW"), (d["room_code"], "Nobody", pw), ("ZZZZZZ", "Mumbai", pw)]:
        r = _login(client, room, who, p)
        assert r.status_code == 401
    assert _login(client, d["room_code"], "Mumbai", d["credentials"][1]["password"]).status_code == 401


def test_rate_limit_then_recovers_on_success_path(client):
    main._login_failures.clear()
    d = _create(client)
    for _ in range(main._LOGIN_MAX_FAILS):
        assert _login(client, d["room_code"], "Mumbai", "NOPE-NOPE").status_code == 401
    assert _login(client, d["room_code"], "Mumbai", d["credentials"][0]["password"]).status_code == 429
    main._login_failures.clear()


def test_franchise_names_endpoint(client):
    d = _create(client)
    r = client.get(f"/api/rooms/{d['room_code']}/franchise-names").json()
    assert r["franchises"] == ["Mumbai", "Chennai"]
    assert client.get("/api/rooms/NOPE00/franchise-names").status_code == 404


def test_host_reset_rotates_only_that_franchise(client):
    main._login_failures.clear()
    d = _create(client)
    code = d["room_code"]
    host = {"Authorization": "Bearer " + d["host_token"]}
    old_pw = d["credentials"][0]["password"]
    old_tok = d["franchise_invites"][0]["token"]
    fid = client.get(f"/api/rooms/{code}/snapshot", headers=host).json()["franchises"][0]["id"]
    r = client.post(f"/api/rooms/{code}/host/reset-password", json={"franchise_id": fid}, headers=host)
    assert r.status_code == 200
    new_pw = r.json()["password"]
    assert _login(client, code, "Mumbai", old_pw).status_code == 401
    new = _login(client, code, "Mumbai", new_pw).json()
    assert new["token"] != old_tok
    assert client.get(f"/api/rooms/{code}/me", headers={"Authorization": "Bearer " + old_tok}).status_code == 401
    # the other franchise is untouched
    assert _login(client, code, "Chennai", d["credentials"][1]["password"]).status_code == 200


def test_reset_requires_host(client):
    d = _create(client)
    f = {"Authorization": "Bearer " + d["franchise_invites"][0]["token"]}
    r = client.post(f"/api/rooms/{d['room_code']}/host/reset-password", json={"franchise_id": None}, headers=f)
    assert r.status_code == 403


def test_host_name_is_reserved(client):
    r = client.post("/api/rooms", json={"name": "T", "franchises": ["Host", "Other"]})
    assert r.status_code == 400
