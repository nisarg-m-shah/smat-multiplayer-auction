# Free deployment guide (10-phone SMAT auction)

This configuration is intended to keep hosting cost at $0 using a Render Free web service and a Supabase Free Postgres database. It is suitable for a carefully rehearsed, one-off hobby auction, but it is **not a production reliability guarantee**. Free providers can restart, pause, or impose limits.

## Architecture

- Render Free web service in Singapore: FastAPI app + WebSockets + auction timer.
- Supabase Free Postgres: persistent room, bid, sale, and squad data.
- Exactly one Render instance and one Uvicorn worker. The app's WebSocket manager, timer supervisor, and room locks are in process memory; do not scale horizontally.

## Important free-tier limitations

- Render Free web services spin down after 15 minutes without inbound HTTP traffic or inbound WebSocket messages. The browser app sends a WebSocket heartbeat every 25 seconds while connected, which should keep an active auction awake. The first connection after idle can take about a minute to wake the service.
- Render may restart a Free service at any time. A restart disconnects all phones. On startup, any room that was live is recovered from PostgreSQL and put into **PAUSED** state. The current player, current bid, highest bidder, accepted bid history, completed sales, squads, and purse balances are retained. When the host resumes, the current player gets a fresh full timer (the old remaining seconds are intentionally not restored). All phones should reconnect and receive the recovered snapshot. Rehearse this before the event.
- Render's filesystem is ephemeral. Never use SQLite for hosted data; use Supabase Postgres. Do not rely on files written at runtime.
- Supabase Free currently includes a 500 MB database, but projects with too little database activity over a week may be paused. Open/test it shortly before the event. Free plan does not provide production backups. Export a database backup before and after the auction.
- Render's Free Postgres is deliberately not used because it expires after 30 days.
- Free services have usage limits and no uptime guarantee. Do not use this for an auction where a provider restart would cause unacceptable disruption.

## Step 1: Create Supabase database

1. Create a project at https://supabase.com/ using the Free plan.
2. Choose a region close to India (for example, Singapore if available).
3. Set and securely save the database password.
4. In the Supabase dashboard, select **Connect** and choose **Session pooler** (port 5432). Render is IPv4-only for Supabase connections; the shared session pooler is IPv4-compatible.
5. Copy the connection string. Replace the password placeholder with your actual database password. URL-encode special characters in the password. Do not use the transaction pooler (port 6543) for this persistent SQLAlchemy backend.

## Step 2: Push code to private GitHub repository

1. Extract this ZIP.
2. Create a private GitHub repository.
3. From the project folder, run:

```bash
git init
git add .
git status
```

Check that `.env`, `.venv`, `*.db`, SQLite files, and invite links are NOT staged. Then run:

```bash
git commit -m "Prepare free SMAT auction deployment"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/smat-multiplayer-auction.git
git push -u origin main
```

## Step 3: Create Render Free web service

1. Sign in at https://render.com/ and connect GitHub.
2. Choose **New → Blueprint** and select the private repository.
3. Render reads `render.yaml`; confirm the web service plan says **Free**. This Blueprint creates no Render database.
4. When prompted for `DATABASE_URL`, paste the Supabase **Session pooler** connection string.
5. Apply the Blueprint. Do not upgrade the service or add paid resources.
6. Wait for deployment to finish.
7. Open `https://YOUR-SERVICE.onrender.com/health`. It should report `ok: true` and `database: ok`.

## Step 4: Verify before inviting participants

1. Open the app and create a disposable test room.
2. Open each franchise invite on separate devices.
3. Keep all phones connected and confirm the WebSocket status stays Connected.
4. Test concurrent bids, countdown extension, automatic progression, sold/unsold celebrations, squad updates, pause/resume, undo/redo, unsold-only round, and final results.
5. Disconnect and reconnect a phone. Refresh a phone during a live lot.
6. Rehearse a server restart/redeploy before the event, not during it. During a live test bid, restart the Render service from its dashboard. Verify that the room returns as PAUSED, the exact current player and accepted highest bid are still shown, and the host can resume with the full configured timer. Also repeat immediately after a player is sold; verify the sale and purse deduction appear exactly once.
7. Export the Supabase database before the real auction.

## Auction-day rules

- Deploy well before the auction and do not push code or change settings during the event.
- Keep exactly one Render instance and one Uvicorn worker.
- Keep the Supabase project active; visit the dashboard and make a test query shortly before the event if it has been idle.
- Have a backup communication channel. If the Render service restarts, phones may need to reconnect.
- Treat host and franchise invitation links as passwords. Anyone with a link can use that role.

## Cost expectation

The intended configuration is $0. Supabase Free is subject to inactivity pausing and resource limits. Render Free is subject to spin-down, restarts, and monthly usage limits. Check the provider dashboards and ensure no paid plan/add-on is selected. Do not enter a paid service plan.
