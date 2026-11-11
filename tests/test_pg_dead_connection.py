"""PostgreSQL only: kill the pooled connections underneath the app, then bid.
Run with DB_POOL_PRE_PING=0 to prove the retry (not the pre-ping) is what saves the bid."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as main
from app.db import engine
from app.models import Bid
from helpers import make_auction, PG

pytestmark = pytest.mark.skipif(not PG, reason="needs PostgreSQL (TEST_DATABASE_URL)")


def _kill_pooled_connections():
    with engine.connect() as c:          # a fresh connection that is not one of the idle pooled ones
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        c.commit()


def test_bid_succeeds_after_server_closed_every_pooled_connection():
    with TestClient(main.app) as client:
        a = make_auction(client)
        lot_id = a.current()["lot_id"]
        assert a.bid("MI", 100, lot_id=lot_id).status_code == 200      # warms the pool
        _kill_pooled_connections()
        r = a.bid("CSK", 110, lot_id=lot_id)
        assert r.status_code == 200, r.text
        assert r.json()["highest_bid"] == 110
        assert a.count(Bid) == 2, "exactly one row per accepted bid"
        # and the next one is back to normal speed with no further retries
        assert a.bid("RCB", 120, lot_id=lot_id).status_code == 200
