"""Runs the browser-side tests (tests/js) against a snapshot taken from a real server."""
import json, shutil, subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
import app.main as main
from helpers import make_auction

JS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None or not (JS / "node_modules" / "jsdom").exists(),
                    reason="needs node and `npm install` in tests/js")
def test_client_state_patching_and_ordering(tmp_path):
    with TestClient(main.app) as c:
        a = make_auction(c)
        me = c.get(f"/api/rooms/{a.code}/me", headers=a.H("MI")).json()
        fixture = tmp_path / "fx.json"
        fixture.write_text(json.dumps({"me": me, "snapshot": a.snap("MI")}))
    r = subprocess.run(["node", str(JS / "client.test.js"), str(fixture)], capture_output=True, text=True, timeout=120)
    print(r.stdout, r.stderr)
    assert r.returncode == 0, r.stdout + r.stderr
