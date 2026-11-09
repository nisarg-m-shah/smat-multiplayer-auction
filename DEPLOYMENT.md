# Deploying the SMAT Auction (10 phones)

## Recommended first deployment

This repository includes a Render Blueprint (`render.yaml`) that creates:

- One Render Python web service in Singapore.
- One managed PostgreSQL database in Singapore.
- A generated `APP_SECRET`.
- A single Uvicorn worker.

The app currently keeps WebSocket connections, timer tasks, and per-room locks in process memory. Therefore, for this version, **keep exactly one web-service instance and one worker**. Do not enable autoscaling or add replicas. Ten phones are a small load for one instance; the reason for the single instance is correctness, not raw capacity.

## Deploy

1. Put this project in a private GitHub repository. Include `data/Auction.py`; do not include `.env`, `.venv`, SQLite databases, invite links, or real room tokens.
2. Push the repository to GitHub.
3. In Render, choose **New → Blueprint** and connect the repository containing `render.yaml`.
4. Review the proposed web service and PostgreSQL database. The Blueprint uses paid service/database plans; review the current price shown in Render before applying. Do not downgrade to a sleeping/free web service for the live auction.
5. Apply the Blueprint and wait for the deployment to become Live.
6. Open `https://<your-service>.onrender.com/health`. It should return JSON with `ok: true` and `database: ok`.
7. Open the root URL and create a disposable test room. Do not use a test room as the real auction room.

## Important hosting settings

- Region: Singapore for participants in India.
- One web-service instance only.
- One Uvicorn worker only (`--workers 1`).
- PostgreSQL is the persistent source of truth; do not use SQLite on the hosted service.
- Use the generated `APP_SECRET`; never copy your local development secret into a public repository.
- Keep the database restricted to private networking.
- Keep the web service on a paid always-on plan for the actual auction.

## Ten-phone rehearsal

Use ten separate phones, or a mix of phones and computers, each opening its own franchise invite. Use the host invite only on the host's device. If the host is also bidding, select their franchise before starting.

Run this full rehearsal before the actual event:

1. Create a room with the actual number of franchises and distribute every invite link.
2. Confirm all devices show Connected.
3. Start the auction and verify all devices show the same player and countdown.
4. Place bids from two devices within the same second; verify only one valid next bid is accepted and all screens converge to the same leader and price.
5. Place a bid with roughly one second remaining; verify the extension and synchronized countdown.
6. Let several players sell and go unsold; verify all devices advance automatically and show the same result.
7. Expand another franchise's squad on multiple phones; verify roster and purse updates after sales.
8. Test pause/resume, undo/redo bid, undo/redo latest player (sold and unsold), and the unsold-only round.
9. Turn Wi-Fi off on one phone for 10–20 seconds, restore it, and verify it reconnects and displays the current state.
10. Refresh one phone during a live lot; verify it rejoins without changing the auction.
11. End a disposable test auction and verify all devices reach the same final results screen.

## Auction-day precautions

- Do not deploy code or change environment variables while an auction is live. Deployments restart the service and disconnect WebSockets. The database deadline is persisted and live rooms are recovered on startup, but avoid relying on restart recovery during the event.
- Keep the host invite private. Anyone with it has administrative control. Each franchise invite is also a bearer credential.
- Keep a copy of all invite links securely outside the browser. The raw tokens are shown only once.
- Have a backup communication channel and a named person responsible for the host device/network.
- Take a database backup before the event and after the event.

## Current architectural limit

This version is intentionally a single-instance deployment. The in-memory WebSocket connection manager, timer supervisor tasks, and Python room locks are not shared across multiple instances or worker processes. If you later need multiple server replicas, replace these with shared coordination (for example PostgreSQL advisory/row locks plus a shared pub/sub layer) before scaling horizontally.
