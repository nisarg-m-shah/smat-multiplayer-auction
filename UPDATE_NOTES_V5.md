# SMAT Auction – follow-up update to the supplied v4 ZIP

This package is based on the supplied `smat-multiplayer-auction-v4.zip`.

## Changes

- Host can undo and redo bids while the auction is paused. The auction remains paused after the correction; no countdown runs until Resume.
- A bid is judged by when the server received the request, not when the database transaction finished. The timer waits 650 ms after the deadline before settlement to let a pre-deadline request reach the room lock. A bid that reaches the server after the deadline is still rejected.
- Browser countdowns estimate server clock offset using server timestamps from snapshots and bid confirmations.
- Franchises and squads moved into a tabbed section with Recent activity. The user's own squad is presented in a dedicated premium card within the franchises tab.
- The current leading franchise is named directly beside the bid controls, so the identity of the bidder is visible without switching tabs.

## Checks

- 57 tests passed; 3 skipped because they require PostgreSQL or optional jsdom setup.
- JavaScript syntax check passed.
- Python compile check passed.

Live deployment and real multi-device visual/timing tests have not been performed. The 650 ms arbitration window is a deliberate small settlement delay, not a guarantee against arbitrary network outages or extreme server stalls.
