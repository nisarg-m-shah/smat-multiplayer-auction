# SMAT Auction — Visual Redesign

This update is a visual-only redesign based on the supplied PlayAuctionGame reference screenshots.

## What changed
- Reworked the visual theme around a premium IPL-style dark palette with warm orange accents.
- Updated cards, buttons, form controls, tables, activity feed, franchise cards, timer, bid button, host controls, celebration overlay and final-results styling.
- Added responsive layouts for laptop/desktop, tablet and narrow mobile screens.
- Bundled Inter font files locally so the interface does not depend on Google Fonts or another external font CDN.
- Added cache-busting versions to the CSS and JavaScript URLs.

## What was intentionally not changed
- No API routes, auction rules, bid validation, timer logic, database models, authentication, WebSocket behaviour, or JavaScript auction workflows were changed.
- Host controls remain in their existing location. The separate host-controls relocation request is deferred.
- The sold/unsold transition timing, undo behaviour, and login/password request are deferred.

## Deployment
Replace the corresponding files in the GitHub repository and push to the branch connected to Render. Wait for the deployment to finish, then hard-refresh all browser sessions (`Cmd + Shift + R`).

The ZIP intentionally excludes `.env`, local database files, Git metadata, virtual environments and Python caches. Keep the existing production environment variables configured in Render.
