# Functional update v5 (based on uploaded v4)

## Changes

- Host can undo and redo bids while the auction is paused. These operations preserve the paused state and the stored remaining timer rather than accidentally restarting the clock.
- Host can continue to undo/redo player settlements while paused using the existing settlement history.
- Added a server-side buzzer grace window (`BID_PROCESSING_GRACE_SECONDS`, default 0.65s). Timer settlement waits for this window, and the bid validator accepts a request that reaches the server during it. A bid still must pass the normal room lock, current-player, amount, purse and squad checks. This reduces false missed bids caused by network/DB processing at the deadline; it does not guarantee acceptance for a click that has not reached the server before the grace window closes.
- Countdown display now estimates server/client clock offset from the server timestamp in snapshots, reducing cross-device drift. The server deadline remains authoritative.
- Replaced the vertically stacked lower information panels with two tabs: **Franchises & squads** and **Recent activity**.
- Moved the user's squad into a highlighted premium summary card at the top of the franchises tab, with player chips and a squad count.
- Added a dedicated current-leader strip immediately below the timer, so the leading franchise is visible without opening the franchises tab.

## Validation

- `node --check app/static/app.js`: passed.
- `python -m compileall -q app tests`: passed.
- `python -m pytest -q -rs`: 56 passed, 3 skipped.
- Skips: two PostgreSQL-only tests (no PostgreSQL test database configured) and one frontend test (jsdom dependency not installed).
- No live Render/Supabase deployment or multi-device browser test was performed.
