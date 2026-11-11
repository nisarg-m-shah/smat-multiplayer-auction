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
* The server now stamps `app.js`/`style.css` URLs in `index.html` with a hash of their content and serves static files with `Cache-Control: no-cache`, so a deploy can never leave browsers on an old frontend. Everyone must reload the page once after deploying.

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

## Update 2 (after live-test report: few bids accepted, timers out of step)
* Not reproduced locally (10 real browsers, 75 ms injected DB latency: clients stayed in sync, timers within ~0.2 s). Likely cause found by review: an early event (e.g. the bidder's own HTTP ack overtaking the WebSocket event for the previous bid) made the browser fetch a full snapshot; with many phones and a large pool that can flood the server and DB pool. Now early events are held up to 1.5 s and applied in order; recovery snapshots are limited to one per 3 s per browser.
* Removed the server-clock timer correction (timers use device clock vs `ends_at`, exactly as your original).
* Rejection messages now end with `[status CODE request_id]` so a failure can be diagnosed from a screenshot; match `request_id` to the Render `bid_timing` line (`reason=`).

## Update 3 (root cause of the live-test failure, from your Render logs)
Server side was healthy: accepted bids `total_ms` ≈ 365–393 ms, `db_stmts=3`, broadcast ≈ 1 ms. The failures were caused by browsers still running the OLD cached `app.js` (your page loaded `app.js?v=dd69e53-fix1`; my `?v=3` edit never matched your index.html, and no `/static/app.js` request appears after the redeploy). The old frontend ignores `BID_ACCEPTED` events, so other clients kept showing the old price, bid it again, got `STALE_BID`, and only then fetched a snapshot. Fixed by server-side content-hash stamping (above). If you merged files by hand, keep `index.html` from this zip or leave it as is: the server rewrites the asset URLs either way.


## Update 4 — premium look, result pause, undo fix, team+password login

**1. Premium IPL look (CSS + small markup/JS touches).** New dark black/amber theme, gradient accents,
Oswald/Inter type, team avatars on franchise cards, a draining timer bar that turns red in the last 3 s,
colour-coded activity feed, restyled result banner, phone-first spacing. All element IDs, the layout and all
bidding logic are unchanged. (Fonts load from Google Fonts; without internet the system fonts are used.)

**2. Result banner then next player.** After a player is settled the server opens the next lot with its timer
starting `RESULT_PAUSE_SOLD_SECONDS` (default 4) / `RESULT_PAUSE_UNSOLD_SECONDS` (default 3) later. During that
pause every client shows "Next player coming up…" (no name, no price, no bid button) and the banner is cut
short to end exactly when the player is revealed. The server also rejects bids in that window
(`PLAYER_NOT_STARTED`), so a hidden player can never be bid on. Set both env vars to 0 to switch it off.

**3. Host controls** moved above the bidding panel. Franchise picker and the unsold-only round are under
"More host options" (collapsed).

**4. Undo last player.** The player goes back on the block at his last highest bid, with the bid history kept
(Undo last bid still works) and the room PAUSED so the host can check before pressing Resume. Whoever was
already live as the next player is put back at the front of the queue (their bids, if any, are cleared) and
starts fresh after the restored player is settled. "Redo last player" works until Resume is pressed.

**5. Login with team + password.** Rooms now get short passwords (`XXXXX-XXXXX`, no look-alike characters) for
every franchise and the host. Login = room code + team + password. The password IS the bearer credential, so
bidding, WebSocket and every other request authenticate exactly as before (no extra queries, same ~4 round
trips per bid). No schema change; old rooms and old long invite links keep working.
- Stays signed in: credential kept per tab (sessionStorage) and per device (localStorage). Reload, lost
  connection, closed tab: still signed in. Network errors never sign anyone out; only Log out, or a password the
  host reset, does. During an outage the page keeps retrying.
- Host screen: "Team logins" table with Copy-message buttons, "Copy all", and "New password" per team
  (stored on the host's device; the server only keeps a hash).
- Room code is no longer shown in the page header; it is only needed at login (and shown in Team logins).
- Failed logins are throttled (15 wrong tries / 5 min / IP).

**6. Dead-connection retry.** If the first database read of a bid fails because the pooled connection is dead,
the server discards it and retries once on a fresh one (nothing has been written yet, so no double bids).
Verified on real PostgreSQL by terminating every pooled connection with `DB_POOL_PRE_PING=0`: the next bid
was accepted and recorded exactly once. `DB_POOL_PRE_PING` stays ON by default (~70 ms per bid) — the retry
is an extra safety net, not a reason to change the default.

**Tests:** 58 pass on PostgreSQL 16 (pre-ping off), 56 pass + 2 skipped on SQLite. New: result pause, undo
scenarios (Sanju/Jaiswal), login, throttling, legacy tokens, password reset, retry, dead PG connection.
Two existing undo tests were updated because "Undo last player" now restores the last bid instead of
re-queueing at base price.

**Known limits:** a password reset does not drop a browser that is already connected (its next page load is
refused); a team that opens the site on a new device needs its password again; the first Google-Fonts load
needs internet.

## v4 follow-up update (November 2026)

**Paused-auction corrections.** Undo/redo bid now remains available when the room is PAUSED. The operation keeps the room and lot paused, clears the running deadline, and stores a full configured timer for the eventual resume. The host action endpoint cancels any outstanding timer supervisor when a correction leaves the room paused.

**Last-second bid arbitration.** The bid endpoint captures the request-arrival timestamp before database work. Deadline eligibility is evaluated against that timestamp rather than the time after authentication, lock acquisition, and database writes. The timer supervisor waits an additional 650 ms arbitration window after the displayed deadline before attempting settlement, allowing a request received before the deadline to acquire the room lock. Requests that actually arrive after the deadline remain rejected. Tune with `BID_DEADLINE_ARBITRATION_MS` if needed.

**Cross-device countdown alignment.** Browsers estimate server clock offset from server timestamps on snapshots and bid confirmations, then render the countdown against the corrected clock. This reduces clock-skew differences; network jitter can still cause small display differences.

**Auction details layout.** Franchises and squads, including a premium own-squad card, now live in a two-tab section alongside Recent activity. The leading franchise is also named explicitly beside the bid controls, rather than relying only on the highlighted franchise card.

**Validation:** `python -m pytest -q -rs` → 57 passed, 3 skipped (PostgreSQL-only and optional jsdom checks); `node --check app/static/app.js` and `python -m compileall -q app tests` passed. No live Render/Supabase or multi-device browser test was performed in this update.
