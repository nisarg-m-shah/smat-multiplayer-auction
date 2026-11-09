# Auction responsiveness and bidding correctness

## What changed in this build

- A bid is the exact amount displayed when the participant clicks. The server rejects an outdated amount with HTTP `409` and returns the latest bid, leading franchise, next required amount, timer deadline and room version. It never silently raises a submitted bid.
- The room lock covers the fresh state read, validation, write and database commit. Timer settlement and host actions use the same room lock, preventing a bid from racing a timer settlement within the single application process.
- The successful bid endpoint returns a compact `BID_ACCEPTED` payload rather than constructing the full room snapshot. The same compact update is broadcast to other connected clients.
- The browser applies compact bid updates in place. It ignores old versions and requests a full snapshot if it detects a version gap or receives an update for a different lot.
- Full snapshots remain the recovery mechanism for initial page load, WebSocket join/reconnect and explicit resynchronisation. A newly connected WebSocket receives its own snapshot rather than triggering a broadcast to every existing participant.
- Synchronous SQLAlchemy work for bids and host actions runs in worker threads, so it does not block the FastAPI event loop.
- WebSocket sends are concurrent and bounded by a timeout; a disconnected/slow socket cannot roll back a committed bid.
- Bid logs include request ID, submitted and accepted amounts, room version, authentication, lock wait, write, commit and total timings. No invite tokens are logged.

## Deployment constraints

The room lock is process-local. Keep exactly one Uvicorn worker and one Render application instance. Do not scale horizontally until the lock is replaced by PostgreSQL row/advisory locking or another shared coordination mechanism. The database commit remains the point at which a bid becomes accepted.

## Verification

Automated tests cover the existing room rules and undo/redo behavior, plus exact-amount stale-bid rejection, latest-price feedback, and simultaneous equal-amount bids. These tests run against an isolated SQLite database by default. They do not prove PostgreSQL/Supabase behavior under production network latency, nor do they replace a multi-device hosted test.

After deploying to a staging/test room, verify:

1. Two franchises click the same displayed amount nearly simultaneously: exactly one is accepted; the other receives a stale-bid message and the new required amount.
2. The rejected participant must click again to submit the new amount.
3. All connected clients converge on the same leader, amount and timer.
4. Let a timer expire while a bid is being submitted; the result must be either a committed bid followed by settlement, or a rejected bid after settlement, never a lost/phantom bid.
5. Disconnect and reconnect a browser; it must recover from a full snapshot.

The local tests do not establish a specific Render latency target. Use the new `bid_timing` log entries after deployment to measure actual performance against Supabase.
