"""Local bid-latency benchmark (NOT a substitute for measuring on Render).

Runs a short auction through the real FastAPI app against a real database and
injects a fixed network round-trip delay (default 75 ms, the value implied by the
Render->Supabase `commit_ms` of ~72 ms in the supplied logs) into every database
round trip: each statement, each COMMIT and each pool pre-ping.

It reports, for each accepted bid, how many round trips it took and how long the
HTTP request took, so changes to the query flow can be compared fairly.

Usage (from the project root):
    DATABASE_URL=postgresql://user@127.0.0.1:5432/db python tools/bench_bid.py --rtt-ms 75 --bids 12
Works against the pre-optimisation tree too (it sends the legacy body shape as well).
"""
import argparse, os, statistics, sys, time, logging, io

ap = argparse.ArgumentParser()
ap.add_argument("--rtt-ms", type=float, default=75.0)
ap.add_argument("--bids", type=int, default=12)
ap.add_argument("--root", default=".", help="project root containing the app/ package")
args = ap.parse_args()
sys.path.insert(0, os.path.abspath(args.root))

from sqlalchemy import event as sa_event
from fastapi.testclient import TestClient
from app import db as dbmod
from app.main import app

RTT = args.rtt_ms / 1000.0
counter = {"n": 0, "stmts": []}
enabled = {"on": False}
eng = dbmod.engine

@sa_event.listens_for(eng, "before_cursor_execute")
def _stmt(conn, cur, statement, params, ctx, many):
    if enabled["on"]:
        counter["n"] += 1; counter["stmts"].append(statement.split("\n")[0][:70]); time.sleep(RTT)

@sa_event.listens_for(eng, "commit")
def _commit(conn):
    if enabled["on"]:
        counter["n"] += 1; counter["stmts"].append("COMMIT"); time.sleep(RTT)

@sa_event.listens_for(eng.pool, "checkout")
def _checkout(dbapi_con, rec, proxy):
    if enabled["on"] and getattr(eng.pool, "_pre_ping", False):
        counter["n"] += 1; counter["stmts"].append("PRE-PING"); time.sleep(RTT)

log_buf = io.StringIO()
h = logging.StreamHandler(log_buf); h.setLevel(logging.INFO)
logging.getLogger("smat-auction").addHandler(h)

c = TestClient(app)
r = c.post("/api/rooms", json={"name": "bench", "franchises": ["MI", "CSK", "RCB"], "timer_seconds": 25,
                               "min_squad": 1, "max_squad": 25, "bid_extension": True,
                               "players": [{"name": f"P{i}", "state_team": "X", "role": "BAT", "base_price": 100} for i in range(5)]})
r.raise_for_status(); d = r.json(); code = d["room_code"]
tok = {x["franchise"]: x["token"] for x in d["franchise_invites"]}
H = lambda n: {"Authorization": "Bearer " + tok[n]}
c.post(f"/api/rooms/{code}/start", headers={"Authorization": "Bearer " + d["host_token"]}).raise_for_status()

names = ["MI", "CSK", "RCB"]
rows = []
for i in range(args.bids):
    who = names[i % 3]
    snap = c.get(f"/api/rooms/{code}/snapshot", headers=H(who)).json()
    cp = snap["current_player"]
    enabled["on"] = True; counter["n"] = 0; counter["stmts"] = []
    t0 = time.perf_counter()
    resp = c.post(f"/api/rooms/{code}/bid", headers=H(who),
                  json={"amount": cp["next_bid"], "lot_id": cp["lot_id"]})
    dt = (time.perf_counter() - t0) * 1000
    enabled["on"] = False
    if resp.status_code != 200:
        print("bid failed", resp.status_code, resp.text[:200]); sys.exit(1)
    rows.append((dt, counter["n"], list(counter["stmts"])))
    time.sleep(0.05)

ms = [r[0] for r in rows]; n = [r[1] for r in rows]
print(f"rtt_injected_ms={args.rtt_ms}  bids={len(rows)}")
print(f"bid request ms: median={statistics.median(ms):.0f} min={min(ms):.0f} max={max(ms):.0f}")
print(f"DB round trips per accepted bid: median={statistics.median(n):.0f} min={min(n)} max={max(n)}")
print("round trips of one steady-state bid:")
for s in rows[-1][2]: print("   ", s)
print("--- bid_timing log lines (last 3) ---")
for line in [l for l in log_buf.getvalue().splitlines() if "bid_timing" in l][-3:]: print(line)
