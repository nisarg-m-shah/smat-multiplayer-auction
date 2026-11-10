# Functional update on top of the premium IPL redesign

## Implemented

1. **Result sequencing:** Sold/unsold result banners are serialized. The next player's details are not rendered until the relevant banner has completed.
2. **Host controls:** Pause/resume, undo/redo, finish and unsold-round controls are positioned above the live auction/bidding grid.
3. **Undo after auto-advance:** The latest sold player reopens immediately at its last accepted bid and leader. The already-selected next player is parked with its existing bid history and remaining timer, then restored after the reopened player is settled. Redoing the sale restores the parked player.
4. **Dead connection retry:** The bid transaction gets one retry for a recognized database connection failure only when COMMIT has not been attempted and rollback has occurred. An ambiguous commit is never retried, avoiding accidental duplicate acceptance.
5. **Room-code display:** The room code is no longer shown in the visible room bar or room-creation heading. The room identifier remains in private URLs and API routes.
6. **Franchise login/session:** Added a franchise-name + password login endpoint. The password is the existing high-entropy invite token, so no schema migration or plaintext password storage is needed. Host can use the name `Host` and host token. The browser stores the token in local storage scoped by room and removes it from the URL after session establishment.

## Authentication model and trade-offs

This is a password-style login over the existing invite credential model, not a new user-account system. The host still generates and distributes each franchise's unique random token; that token acts as the password. The server stores only its existing hash. This avoids a database schema migration and avoids storing user-chosen low-entropy passwords. The browser's local-storage session persists across reloads and browser restarts on that device. Anyone with access to the browser profile can use that session; clear site data to sign out.

## Verification

- `python -m pytest -q`: 47 passed, 2 skipped.
- `node --check app/static/app.js`: passed.
- `python -m compileall -q app tests`: passed.
- Two skips are the existing browser-side jsdom tests because jsdom is not installed in this build environment.
- A live Render/Supabase multi-device test has not been run.

## Deployment

No schema migration is required. Deploy the complete project ZIP or commit the changed files to the branch connected to Render. Keep one Render instance and one Uvicorn worker, as the room locks and WebSocket registry are process-local. Test the full undo/redo sequence and login on separate browsers before a real auction.
