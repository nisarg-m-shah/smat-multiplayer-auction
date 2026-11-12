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

## Latest update
- **Result hold**: the next player opens only after the SOLD/UNSOLD banner has been shown (server-side, so every screen stays in sync). `RESULT_HOLD_SOLD` (default 5s) and `RESULT_HOLD_UNSOLD` (default 3.5s); set 0 to disable.
- **Premium IPL redesign** of the whole UI (self-hosted fonts, monogram team badges, result stage with confetti, mobile bid dock). Layout and logic unchanged.
- **Host controls** moved to the top, directly above the bidding.
- **Undo last player** now restores the player at their last highest bid, then continues with the already-chosen next player (whose in-progress bids are discarded and who returns to the front of the queue). A paused room stays paused.
- **Login by room code + franchise name + password** (host signs in as "Host"). Old invite links still work. Passwords are PBKDF2-hashed; sessions are additive (any number of devices), persist in the browser, and are cleared only on sign-out or a host password reset, never on a network drop. Host can issue a new password per franchise from the Access panel. Login is rate limited (12 wrong tries / 5 min per IP+room). The bid path is unchanged.

## Update: paused fixes, late-bid grace, clock sync, tabs
- **Undo/redo while paused**: undo/redo bid and undo/redo player all work in a paused room, which stays paused. The restored player gets a full timer held until Resume.
- **Pause timer floor**: pausing never leaves less than half the timer (`timer_seconds / 2`); more is kept if more remained. The paused clock shows what will be on it at resume.
- **Late-bid grace**: bids are judged by when the request *arrived* at the server, not when the database finished, and bids up to `BID_GRACE_SECONDS` (default 0.8) after the deadline still count. The player is sold only after grace plus `SETTLE_SLACK_SECONDS` (0.25). Set `BID_GRACE_SECONDS=0` to disable.
- **Clock sync**: browsers now measure their offset against the server (`GET /api/time`, lowest-latency of several samples, refreshed every 15 s and on reconnect/focus), so a phone with a wrong clock shows the same countdown as everyone else.
- **UI**: Franchises / Activity tabs next to (mobile: under) the bid box; "Your team" card (purse, squad, spent, role counts, players with roles) lives at the top of Franchises and is not repeated in the list; the bid box shows the highest bidder with their badge.

## Update: final screen, home copy, dropdown login, compact host bar
- **Final screen** puts your own team first with a gold "Your team" highlight.
- **Home page** copy shortened; developer-only note about `Auction.py` removed from the UI.
- **Franchise dropdown** on the join form (filled in from the room code). If the host plays as a franchise, that franchise *is* the host login: one entry "Name (Host)", signed in with the host password; no separate "Host" option and no separate franchise password to hand out. The credentials card and Access panel update accordingly.
- **Host bar** is a single row of buttons; status pill sits at the left and a "More" button opens a drawer (My franchise, Unsold-only round, Access & passwords), each section expandable.

## Update: full-page result, sounds, unsold list
- **SOLD / UNSOLD** now covers the whole page (dimmed, blurred) for the server's hold time, with a real celebration on SOLD: two confetti cannons from the bottom corners plus a shower of paper (rects, strips, dots; fewer pieces on phones and with reduced-motion). UNSOLD gets a few slow grey scraps. The host bar stays on top and clickable, so Undo is still one tap away.
- **Sound**: a two-note ping on every accepted bid, a soft tick each second over the last 10 seconds (higher and louder in the last 3), an urgent double buzzer at 0, plus the SOLD fanfare / UNSOLD sigh. A speaker button in the top bar mutes everything and is remembered per device. Browsers only allow audio after a first tap, which happens naturally at sign-in.
- **Unsold-only round** list now shows each player's role and state team.

## Round 5

- **Unsold sound**: a descending "sad trombone" (sawtooth) plus a low thud when a player goes unsold.
- **Pool tab** (third tab beside Franchises / Activity): Upcoming / Sold / Unsold lists with counts, search by player, team or role, and the current lot marked "On the block". Backed by `GET /api/rooms/{code}/pool`.
- **Passwords at the bottom of the page** (host only): show/hide, copy invite, copy all invites, and issue a new password per franchise. Passwords are now derived from the per-franchise nonce (HMAC), so they can be viewed again without storing plaintext. Rooms created before this update only have a hash, so they show "Not viewable" and need "Issue password". Endpoint: `GET /api/rooms/{code}/host/credentials`.
- **"I'm out"**: a button beside Place Bid. Franchises that have stepped out are shown to everyone in a slim strip. The highest bidder can't step out, and a franchise that bids again is automatically back in. When every franchise that could still bid is out, the clock drops to `ALL_OUT_SECONDS` (default 1.5 s); the player is sold if one franchise is left (the leader) and unsold if none is. Rejoining restores a fair clock (at least half the timer). A host without a chosen franchise has no "I'm out". Endpoint: `POST /api/rooms/{code}/pass`; new table `lot_passes` (created automatically).

## Round 6

- **Setup pool is back.** Round 5's Pool-tab endpoint reused the host setup list's `/pool` URL, so the setup table received the wrong data and rendered empty. The tab now uses `/auction-pool`. Regression test added.
- **Host franchise picker** on the setup screen is now tappable chips (Host only + every franchise) instead of a dropdown that could collapse to a single option.
- **Passwords only at the bottom.** The "Auction created" card at the top is gone; a one-line hint with a jump link points to **Franchise passwords** at the bottom, which opens automatically after creating a room.
- **Pick the next player.** Pool tab: tap **Next** beside any upcoming/unsold player (a player already queued is moved, not duplicated; sold players are refused). More > **Next player: add or pick** adds a brand-new player live, as next up or at the end of the queue. If the round had run out of players, the chosen player opens immediately. Endpoint: `POST /api/rooms/{code}/queue-player`. The queue label shows "Next: <player>".
- **End auction** is a small button inside More (still with a confirmation).
- **Unsold-only list** is now one tidy card per player (name, role/team, base, optional new base).
- **"Everyone out"** no longer collapses the clock: it trims it to half the timer (never lengthening it), so people can rejoin. `ALL_OUT_FRACTION` replaces `ALL_OUT_SECONDS`.
- **Importance timer.** In rooms with a 25 s timer or more, a player with importance weight > 1 gets a longer clock: up to half the timer extra for the pool's highest weight, scaled logarithmically (weight 4 of max 16 gets about +6 s), rounded to whole seconds. Bid extensions, pause floor, undo/redo and "everyone out" all use that player's own window. A "+13s clock" chip shows on such players. Stored per lot in `lots.timer_seconds` (added automatically to existing databases at startup). Tunable via `IMPORTANCE_MIN_TIMER` and `IMPORTANCE_TIMER_BONUS`.
- Mobile polish: compact setup table (role and state under the name), top-bar name chips no longer overlap.
