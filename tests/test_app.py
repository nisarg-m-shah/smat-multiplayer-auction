from fastapi.testclient import TestClient
from app.main import app

def test_health():
    client = TestClient(app)
    assert client.get("/health").json()["ok"] is True


def test_franchise_name_password_login_exchanges_private_invite_token():
    from helpers import make_auction
    client = TestClient(app)
    a = make_auction(client, start=False)
    password = a.tokens["MI"]
    response = client.post(f"/api/rooms/{a.code}/login", json={
        "franchise_name": "mi", "password": password
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"token": password, "is_host": False, "franchise": "MI"}
    me = client.get(f"/api/rooms/{a.code}/me", headers={"Authorization": "Bearer " + response.json()["token"]})
    assert me.status_code == 200 and me.json()["franchise"]["name"] == "MI"


def test_franchise_login_rejects_wrong_password_or_name():
    from helpers import make_auction
    client = TestClient(app)
    a = make_auction(client, start=False)
    response = client.post(f"/api/rooms/{a.code}/login", json={
        "franchise_name": "MI", "password": a.tokens["CSK"]
    })
    assert response.status_code == 401


def test_host_can_exchange_host_password_for_persistent_session():
    from helpers import make_auction
    client = TestClient(app)
    a = make_auction(client, start=False)
    response = client.post(f"/api/rooms/{a.code}/login", json={
        "franchise_name": "Host", "password": a.host_token
    })
    assert response.status_code == 200
    assert response.json()["is_host"] is True
