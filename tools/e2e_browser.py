"""End-to-end check: 3 real browsers (Playwright/Chromium) against a running server.
usage: python tools/e2e_browser.py http://127.0.0.1:8765"""
import sys, json, time, urllib.request
from playwright.sync_api import sync_playwright

base = sys.argv[1].rstrip("/")
def call(path, body=None, token=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    return json.load(urllib.request.urlopen(req))

d = call("/api/rooms", {"name": "e2e", "franchises": ["MI", "CSK", "RCB"], "purse": 5000, "timer_seconds": 25, "min_squad": 1, "max_squad": 25,
                        "players": [{"name": f"P{i}", "state_team": "X", "role": "BAT", "base_price": 100} for i in range(4)]})
code, tok = d["room_code"], {x["franchise"]: x["token"] for x in d["franchise_invites"]}
call(f"/api/rooms/{code}/start", {}, d["host_token"])
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pages = {}
    for n in ("MI", "CSK", "RCB"):
        pg = b.new_context().new_page()
        pg.on("pageerror", lambda e, n=n: errors.append((n, str(e))))
        pg.on("console", lambda m, n=n: errors.append((n, m.text)) if m.type == "error" else None)
        pg.goto(f"{base}/?room={code}&token={tok[n]}")
        pg.wait_for_function("document.getElementById('connection') && document.getElementById('connection').textContent.includes('Connected')", timeout=10000)
        pages[n] = pg
    txt = lambda n, i: pages[n].inner_text("#" + i).strip()
    # tag the franchise grid DOM nodes: they must survive bids (no full re-render)
    for pg in pages.values():
        pg.evaluate("document.querySelector('#franchises details').dataset.keep='1'; document.querySelector('#franchises details').open=true")
    t0 = time.time(); pages["MI"].click("#bid-button")
    for n in pages:
        pages[n].wait_for_function("document.getElementById('current-bid').textContent.trim()==='100'", timeout=5000)
    print(f"all 3 browsers show 100 after {1000*(time.time()-t0):.0f} ms (local, no injected latency)")
    print("MI feedback:", txt("MI", "bid-feedback"), "| MI button disabled (leader):", pages["MI"].is_disabled("#bid-button"))
    # CSK clicks while its screen already shows 110 -> accepted at 110
    pages["CSK"].click("#bid-button")
    for n in pages:
        pages[n].wait_for_function("document.getElementById('current-bid').textContent.trim()==='110'", timeout=5000)
    # stale bid from the browser: freeze RCB's view at 110, make MI bid 120, then RCB presses with the stale price
    pages["RCB"].evaluate("window.__ws_freeze=true; document.getElementById('bid-button').dataset.x=1")
    pages["RCB"].evaluate("""() => { const orig = window.applyIncoming; window.applyIncoming = () => {}; window.__orig = orig; }""")
    pages["MI"].click("#bid-button")
    pages["MI"].wait_for_function("document.getElementById('current-bid').textContent.trim()==='120'")
    time.sleep(0.5)
    print("RCB still shows (frozen):", txt("RCB", "current-bid"), "| next:", txt("RCB", "next-bid"))
    pages["RCB"].click("#bid-button")
    time.sleep(0.8)
    print("RCB after stale bid ->", txt("RCB", "bid-feedback"))
    for n in pages:
        print(n, "current-bid", txt(n, "current-bid"), "|", txt(n, "leading"), "| keep-node:", pages[n].evaluate("document.querySelector('#franchises details').dataset.keep==='1' && document.querySelector('#franchises details').open"))
    print("browser stats MI:", pages["MI"].evaluate("window.__bidStats"))
    b.close()
print("console/page errors:", errors or "none")
