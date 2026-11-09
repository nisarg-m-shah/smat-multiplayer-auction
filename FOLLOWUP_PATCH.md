# Follow-up reliability patch (based on GitHub commit dd69e53)

## Changes
- Timer supervisor now logs full exceptions and retries transient database failures with bounded backoff (1s to 10s).
- Timer supervisor follows the persisted deadline after a bid extension or after opening the next lot, rather than relying on nested task cancellation/re-arming.
- Cancellation remains explicit and is not treated as a database failure.
- Frontend feedback normalizes string and object-shaped API errors, preventing `[object Object]` from being rendered.
- Stale/non-live bid rejections trigger an authoritative snapshot refresh.
- Static JS/CSS URLs carry a new version query so browsers do not keep an older frontend bundle after deployment.
- Added a regression test proving the timer retries after a transient settlement failure.

## Verification
- `python -m pytest -q`: 13 passed.
- `node --check app/static/app.js`: passed.
- `python -m compileall -q app tests`: passed.

## Deployment
1. Replace the corresponding files in the GitHub `main` branch with this patch.
2. Commit and push; let Render finish deploying before opening the auction.
3. Hard-refresh all browser sessions (Cmd+Shift+R on macOS).
4. Create a fresh test room and test bidding, expiry/auto-advance, pause/resume, and two simultaneous bidders.
5. Check Render logs for `timer_supervisor_error`. A transient database failure should now be visible and retried.

## Important operational notes
- Keep one Render instance and one Uvicorn worker: room locks and timer supervisors are process-local.
- A Render restart intentionally pauses any live auction. The host must resume it after deployment/restart.
- These tests use the local test database. A live Supabase/Render multi-browser test is still required.
- Do not commit `.env`, database files, `.venv`, or `.git` into a source ZIP/repository export. Rotate any credentials that may have been exposed in a shared `.env`.
