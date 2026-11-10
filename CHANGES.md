# Bid-latency & real-time sync update

## Root cause (measured, not guessed)
Render→Supabase costs ~75 ms per database round trip (matches your `commit_ms` ≈ 72 ms). A bid used **~24 round trips**:
auth ≈ 6 (room + franchise looked up 3×, plus pool pre-ping), validation/writes ≈ 10, snapshot ≈ 7 — i.e. 24 × ~75 ms ≈ 1.8–2.0 s.
Reproduced locally with a 75 ms delay injected into every DB round trip (`tools/bench_bid.py`): original = **median 1,887 ms, 24 round trips**
(auth 382 / validate+write 765 / commit 76 / snapshot 535 ms vs. your Render logs 433 / 724–747 / 72–74 / 727–756).

## What changed
| Area | Change |
|---|---|
| **Exact-amount bids** | `POST /api/rooms/{code}/bid` now **requires** `amount`. The server never raises it. Anything other than the exact required price → **409 `STALE_BID`** with the latest price/leader/deadline. Optional `lot_id`/`player_id` pin the bid to the player on screen; optional `franchise_id` is only *checked* against the token's franchise. |
| **Rejection codes** | `STALE_BID`, `ALREADY_HIGHEST_BIDDER`, `INSUFFICIENT_PURSE`, `AUCTION_NOT_LIVE`, `PLAYER_ALREADY_SETTLED`, `FRANCHISE_NOT_AUTHORISED` (403), plus `TIMER_EXPIRED`, `SQUAD_FULL`; 503 `BID_NOT_SAVED` / `BID_NOT_CONFIRMED` for DB failures. Body: `{accepted, code, detail, request_id, state}` (`detail` is still a plain string). |
| **Round trips** | Authentication + live state in **one** query; the credential is re-verified against the DB on every bid. Writes fused into one statement on PostgreSQL. **24 → 4 round trips.** No snapshot on the bid path. |
| **Compact events** | Accepted bid returns/broadcasts `BID_ACCEPTED` (~400 bytes) instead of a snapshot. Full snapshots remain for load/join/reconnect/host actions/settlement. |
| **Versioning** | Every state change bumps `room.version` by exactly 1 (previously timer settlement and nominate did not). Client ignores `version <= local`, patches only `== local+1`, and fetches a snapshot on any gap/mismatch. |
| **Concurrency** | One room lock (keyed by room code) now covers bids, timer settlement **and host actions**, from state read to commit, with the broadcast queued inside the lock so order == commit order. Lock creation made thread-safe. The lot UPDATE is also guarded in SQL (`WHERE bid_sequence = <validated value>`), so a double-accept is impossible even if two processes ever overlapped. |
| **Timer** | Settlement can no longer be cancelled half-way (a bid re-arming the timer used to be able to swallow the settlement broadcast). Bids re-arm with the committed deadline (no DB read). Bid after deadline → `TIMER_EXPIRED` and settlement is triggered. DB error during settlement → retry in 3 s. |
| **WebSockets** | Per-client queue + writer: a slow/dead client delays only itself (5 s write timeout, 256-message cap, then dropped → reconnects and recovers). Serialised once per message. A (re)connect now sends the snapshot to **that client only** (before: to the entire room). WS auth no longer blocks the event loop. |
| **Frontend** | Single `state`, patched in place: highest bid, leader, next bid, timer, button, activity feed, undo/redo buttons, leader highlight. Franchise cards/host panel are *not* rebuilt (expanded squads stay open). Bid button disables while in flight and when you lead. Amount sent = price on the button at *pointer-down*. Rejection updates the screen instantly. Timer uses server time offset. Network error ⇒ "not confirmed" + resync (never assumes success). Shows ack time ("· 312 ms") and keeps `window.__bidStats`. |
| **Auth elsewhere** | `auth_context` 2 → 1 query. Unknown room → 404 (was 500). |
| **Instrumentation** | `bid_timing` line per bid: `request_id room submitted outcome accepted_amount/reason version auth_ms lock_wait_ms read_auth_ms validate_ms write_ms commit_ms response_ms broadcast_enqueue_ms total_ms db_stmts`. New `bid_broadcast` line: `recipients ok failed max_ms avg_ms` (commit→socket write, server side). `BID_TRACE_SQL=1` adds per-statement timings. No tokens are logged. Client sends optional `X-Request-Id`. |

## Modified / new files
Modified: `app/main.py`, `app/engine.py`, `app/db.py`, `app/static/app.js`, `app/static/index.html` (cache-bust `?v=3`), `tests/conftest.py`.
New: `app/perf.py`, `tests/helpers.py`, `tests/test_bidding.py`, `tests/test_realtime.py`, `tests/test_frontend_js.py`, `tests/js/*`, `tools/bench_bid.py`, `tools/e2e_browser.py`, `CHANGES.md`.
Unchanged: models/schema, seed, config, security, render.yaml, existing tests (all still pass unmodified).

## Database / deployment
* **No schema change, no migration.** `.env` untouched.
* Deploy as usual (push → Render auto-deploy). Keep **1 worker, 1 instance**. Don't deploy mid-auction (see limitations).
* Optional env vars (all have safe defaults): `BID_FUSED_WRITE=0` (fall back to 4 separate write statements), `DB_POOL_PRE_PING=0` (saves ~75 ms/request but risks an error on the first bid after an idle disconnect — only after measuring), `BID_TRACE_SQL=1` (per-statement timings in `bid_timing`).
* Browsers with the old `app.js` cached would send no `amount` → 422; the `?v=3` query string forces a refresh. Ask everyone to reload once.

## Tests (run: `pip install pytest httpx && pytest`; JS tests need `cd tests/js && npm install`)
Existing 9 tests: pass, untouched. New: A normal, B stale, C concurrent (2- and 3-way, with slowed commits), D same-franchise, E purse (+min-squad reserve), F timer (expired, settled, settle-vs-in-flight-bid, re-arm), G three WS clients + no snapshot after bid, H out-of-order/duplicate/stale (JS, jsdom), I reconnect (server + client gap recovery), J DB failure before/at commit, K regression; plus auth/identity, player-pinning, undo→rebid branch, no-snapshot-on-bid-path, log fields/no secrets, slow/dead WS client isolation.
Results: **SQLite 42 passed, 1 skipped; PostgreSQL 16 43 passed** (also with `BID_FUSED_WRITE=0`); real-Chromium 3-client run via `tools/e2e_browser.py` OK.

## Measurements (honest scope)
Local PostgreSQL with 75 ms injected per round trip — **a model of Render→Supabase, not a Render measurement**:
| | round trips | bid request |
|---|---|---|
| Original | 24 | median 1,887 ms |
| New, fused writes (default) | 4 | median 314 ms |
| New, `BID_FUSED_WRITE=0` | 7 | median 542 ms |
Expect roughly this if your real RTT is ~75 ms; **confirm in your Render logs** (`total_ms`, `db_stmts`, `bid_broadcast`). I have not measured on Render/Supabase, so the <500 ms targets are unproven there. If RTT is higher, `db_stmts × RTT` predicts total_ms.

## Remaining limitations
* Single process only (process-local lock + in-memory WebSocket registry + timers). The SQL guard protects bids if two instances overlap during a deploy, but settlement/host actions are not DB-coordinated.
* Player settlement/next-player (`_settle_expired_lot`) still takes ~15 round trips plus a full snapshot (~1–2 s on Render). It's once per player, not per bid; the next target if the gap between players feels slow (snapshot loads every player in the room).
* Broadcast timing is server-side; client-side delivery adds network latency (~one-way RTT).
* Version numbers assume the DB is never restored to an older state while browsers are open.
* The fused PostgreSQL statement was tested on PG 16 locally, not on Supabase; use `BID_FUSED_WRITE=0` if it ever misbehaves.
