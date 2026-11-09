# SMAT Private Multiplayer Auction

A first working version of a private auction room for 2–10 custom franchises. It is a separate application from the scoring dashboard.

## What is included

- Host creates a private room and names 2–10 franchises.
- One private invite link per franchise; no public sign-up.
- Player pool is seeded from the `squads` literal in your copied `Auction.py`.
- Host can include/exclude players, change base price and importance weight, add players, and import a CSV before the auction.
- Live auction with server-side bid validation and WebSocket updates.
- Configurable purse, timer (5/10/25 seconds), minimum squad and maximum squad.
- Bid increments: +10 while current bid is below 200; +20 from 200 to below 500; +25 from 500 onward.
- Bid timer extension: adds half the configured timer to the remaining time, capped at one full timer window.
- Host controls: pause, resume, settle/advance, undo latest bid, undo latest sale, finish auction.
- Unsold players can be nominated by the host for a later round at the same base price; the backend accepts optional price overrides.
- Weighted random player order. Importance weight 1.0 is ordinary chance; higher values make the player more likely to appear earlier.
- SQLite for local use; PostgreSQL supported through `DATABASE_URL`.
- Basic automated tests.

## Important scope notes

This is a functional starter implementation, not a fully audited production service. Before a real 10-hour auction, deploy it, run the multi-browser test checklist, and test recovery on the actual hosting provider.

The current v1 deliberately does not include:
- Snake draft.
- RTM.
- Fantasy scoring or scoring-dashboard integration.
- Player photographs.
- Automatic AI player importance ratings.
- Multiple simultaneous rooms per account.

The host invite is a bearer secret. Anyone who has the host link can control the room. Share it privately and keep it out of public chats.

## 1. Local setup

Use Python 3.11 or newer.

```bash
cd smat-multiplayer-auction
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Copy your supplied `Auction.py` into `data/Auction.py`. The importer reads only the literal `squads = {...}` assignment using Python's AST; it does not execute the rest of the file or import its dashboard dependencies.

Start the app:

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

The first page is the room creator. Enter 2–10 franchise names, purse, timer and squad limits. Save the host and franchise invite links shown after creation. The raw invite tokens are shown only once.

## 2. Player data

By default, every player in `Auction.py` gets:
- Base price: 20.
- Importance weight: 1.0.
- Role and state team copied from `squads`.

No importance ranking exists in the supplied `Auction.py`, so this app does not pretend to know which players are more important. The host can adjust each player's importance weight in the pool manager.

You can also import a CSV with these headers:

```csv
name,state_team,role,base_price,importance_weight
Example Player,Mumbai,BAT,30,2.5
Another Player,Railways,BOWL,20,1.0
```

Higher `importance_weight` increases the player's probability of appearing earlier. A value of 1 is normal; 2 is approximately twice the relative weight of a player with weight 1.

## 3. Auction flow

1. Host creates the room.
2. Host shares the correct invite link with each franchise owner.
3. Host removes unwanted players, adjusts prices/importance, and optionally adds players.
4. Host locks the pool and starts.
5. The first player opens for bidding.
6. Franchise owners press the bid button. The backend calculates and validates the next legal amount.
7. When the server deadline expires, the highest bidder wins. If nobody bids, the player is unsold.
8. Host advances through the queue.
9. When the first round is complete, the host selects nominated unsold players and starts their second-round queue.
10. Host can undo the most recent bid while a player is live, or undo the most recent completed sale.

### Bid increment interpretation

The prompt says increments are 10 from 10–200, 20 after that until 500, and 25 after 500. This implementation interprets that as:
- Current bid < 200: next increment +10.
- Current bid >= 200 and < 500: next increment +20.
- Current bid >= 500: next increment +25.

If you intended a different boundary at exactly 200 or 500, change `increment_for()` in `app/engine.py`.

### Timer extension

After a valid bid, the remaining time increases by half the configured timer, capped at one full configured timer window. For example, with a 10-second timer and 2 seconds remaining, the new timer becomes 7 seconds. With 8 seconds remaining, it becomes 10 seconds.

## 4. Run tests

```bash
pytest -q
```

## 5. PostgreSQL deployment

Set `DATABASE_URL` to a PostgreSQL connection string, for example:

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
APP_SECRET=use-a-long-random-secret
AUCTION_SOURCE=./data/Auction.py
```

Run one backend instance and one Uvicorn worker for v1. The in-process room lock serializes bids and timer/host mutations only within that process; do not horizontally scale this version without replacing it with a database/advisory-lock strategy.

For Render, use a persistent paid instance if uninterrupted uptime is essential. A free instance can sleep or restart. The database deadline is persisted, so the app can recover the current lot after a restart, but a restart can still interrupt the user experience while it wakes up.

Vercel is not required for this first version because the FastAPI server serves the frontend itself. This keeps deployment simpler and avoids separate frontend/backend origins.

## 6. Before the real auction: must-run checklist

- Create a test room with 10 franchises.
- Open every franchise invite in a separate browser/incognito session.
- Place bids from different devices at nearly the same time.
- Test a bid with 1 second remaining.
- Pause and resume during a live player.
- Disconnect a participant and reconnect.
- Restart the backend during a live lot and confirm it recovers.
- Undo a bid and confirm the previous bid is restored.
- Undo a sale and confirm the purse is refunded and player is available again.
- Complete a round, nominate unsold players, and confirm they return at the configured price.
- Confirm a full squad cannot bid.
- Confirm purse cannot go negative.
- Confirm host invite is not shared with franchise owners.

## 7. Simplified explanation of the code

- `models.py`: describes what is stored in the database (rooms, franchises, players, bids, sales).
- `seed.py`: reads your existing `Auction.py` player list and creates the auction player pool.
- `engine.py`: contains the actual auction rules: bid increments, validation, timer, sale, undo and unsold nomination.
- `main.py`: exposes the API routes and WebSocket connection used by the website.
- `static/index.html`: page structure.
- `static/style.css`: visual design.
- `static/app.js`: browser behaviour and live updates.
- `tests/`: automated checks of the auction rules.

## Security warning

The invite-link approach is convenient for a private group, but links are credentials. Anyone who obtains a franchise invite can act as that franchise; anyone who obtains the host invite can administer the auction. Do not post these links publicly. Use HTTPS in deployment. Rotate/recreate a room if a link leaks.


## Latest fixes (undo and auction result celebrations)

- Undo Last Bid now flushes the deactivation of the latest bid before querying the preceding active bid. This is required because the SQLAlchemy session is configured with `autoflush=False`.
- The undo lookup uses bid sequence and database ID for deterministic ordering when sequence numbers are reused after an undo.
- Sale/unsold events now include event IDs and structured payloads in snapshots.
- All connected clients display a brief result overlay when a player is sold or goes unsold. Sales show the player and price with confetti; unsold results use a subdued overlay.
- A short celebratory or descending sound is generated with the browser Web Audio API. Sound availability depends on the browser's audio permissions and prior user interaction.


## Undo/redo and final results

The host controls include undo and redo for bids and completed sales. Redo restores the original bid or sale record, buyer, price, purse deduction, and squad entry; a new action clears the corresponding redo branch. The app adds `redo_available` columns to existing SQLite/PostgreSQL databases automatically at startup.

When the host ends the auction, any currently active player is first settled at the current bid (or marked unsold if there are no bids), then all participants are taken to a final results screen with franchise squads, player roles, state teams, purchase prices, spend, and remaining purse.


## Live auction behaviour (updated)

- Players automatically advance when the configured countdown expires. The host does not need to press a Next Player button. The auction changes to Round Complete when the current queue is exhausted.
- Franchise cards in the live view expand to show the full squad, role and purchase price.
- Before starting, the host can select one franchise to manage as a participant. The selection is locked after the auction starts. The host can then bid for that franchise while retaining administrator controls. Do not share that franchise's invite link with another bidder.
- Undo/redo last player applies to the most recently settled player whether sold or unsold. Undoing a sold player refunds the franchise; redo restores the original transaction.
- The host can select unsold players and start an unsold-only round before the original queue is exhausted. This explicitly skips all remaining unseen queued players, settles the current player first, and starts the selected unsold round immediately.


## 8. Hosted deployment for a 10-phone auction

See [DEPLOYMENT.md](DEPLOYMENT.md). A Render Blueprint and `.python-version` are included. The hosted version uses managed PostgreSQL, one always-on web instance, and one Uvicorn worker. Do not scale to multiple instances/workers until the in-memory WebSocket manager, timer supervisor, and room locks are replaced with shared coordination. Bids are exact-amount instructions: stale amounts are rejected with HTTP 409 and are never auto-increased.


## Free hosting (10-phone test)

See `DEPLOYMENT.md` for the Render Free + Supabase Free setup. The free deployment uses Supabase Postgres because Render Free web services have ephemeral filesystems and Render Free Postgres expires after 30 days. Free hosting can restart or sleep; rehearse all 10 devices before the real auction.


## Restart recovery

Auction state is stored in PostgreSQL. If the server process restarts while a room is live, startup recovery marks that room and its current lot as paused without changing the current player, highest accepted bid, bid history, completed sales, franchise squads, or purse balances. The host must resume manually; the current lot then receives a fresh full timer. Accepted bid requests are committed to the database before the API acknowledges them. Test this with a real Render restart and Supabase database before the live auction.
