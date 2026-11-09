# Auction responsiveness update

This update addresses avoidable server-side latency in live bidding:

- Synchronous SQLAlchemy/PostgreSQL work for bid and host-action requests now runs in a worker thread instead of blocking the FastAPI event loop.
- The timer no longer polls PostgreSQL several times per second. It reads the persisted deadline once, sleeps until that deadline, then performs settlement in a worker thread. A new bid re-arms the timer using the newly committed deadline.
- Snapshot generation for WebSocket joins is moved off the event loop.
- WebSocket broadcasts are sent concurrently, so one slow client does not serialize delivery to every other client.
- Bid responses are returned after the database commit and snapshot; the WebSocket broadcast is scheduled separately so a slow phone cannot delay the bidder's HTTP acknowledgement.
- Snapshot history flags are derived from data already loaded for the snapshot, removing three extra database round trips.
- Squad-size validation uses SQL `COUNT` rather than fetching every sale row.
- The frontend disables the bid button while a bid request is in flight to prevent accidental duplicate submissions.
- Server logs now include `bid_timing` with separate auth, write/validation, commit, snapshot, and total durations. These timings will identify whether remaining delay is in Supabase or application logic.

A bid is still acknowledged only after PostgreSQL commits it. The update does not optimistically show an uncommitted bid as accepted.

## Verification still required

Automated tests and syntax checks are not a substitute for a live multi-device test against the hosted Supabase database. After deployment, inspect Render application logs for `bid_timing` entries and test with several devices. Do not use the live auction until bids register reliably and the accepted bid is consistent on all devices.

## Concurrent bid handling update

The bid endpoint now treats a browser click as an instruction to place the next
valid bid. The server calculates the amount from the latest committed lot state;
it does not trust a potentially stale amount displayed by a bidder's browser.
The per-room lock covers refresh, validation, bid write, and database commit.
This prevents two requests handled by the single Render web-service process
from validating against the same previous bid. The frontend also ignores older
room-version snapshots that arrive after newer ones, avoiding visual rollback
when concurrent HTTP responses complete out of order.

The lock is process-local. This deployment must remain a single application
instance; if the app is later scaled to multiple worker processes or instances,
replace this coordination with a PostgreSQL row/advisory lock or equivalent
cross-process transaction serialization before scaling out.
