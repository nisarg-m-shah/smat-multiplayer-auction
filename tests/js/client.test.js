// Browser-side tests (Tests G "page not rebuilt" and H "out-of-order events").
// usage: node client.test.js fixture.json   (fixture = {me, snapshot} from a real server)
const fs = require("fs"), path = require("path"), assert = require("assert");
const { JSDOM } = require("jsdom");
const fx = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const root = path.resolve(__dirname, "../../app/static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8").replace(/<script[^>]*><\/script>/g, "");
const js = fs.readFileSync(path.join(root, "app.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function boot({ bidResponse } = {}) {
  const calls = [];
  const dom = new JSDOM(html, { url: "http://x/?room=" + fx.snapshot.room.code + "&token=tok", runScripts: "outside-only", pretendToBeVisual: true,
    beforeParse(w) {
      w.alert = () => {}; w.HTMLMediaElement.prototype.play = () => Promise.resolve();
      w.fetch = async (url, opts = {}) => {
        calls.push({ url, opts });
        const reply = (status, body) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(body)) });
        if (url.endsWith("/me")) return reply(200, fx.me);
        if (url.endsWith("/snapshot")) return reply(200, w.__nextSnapshot || fx.snapshot);
        if (url.endsWith("/bid")) return typeof bidResponse === "function" ? bidResponse(opts) : reply(200, {});
        return reply(404, {});
      };
      w.WebSocket = class { constructor() { w.__ws = this; this.readyState = 1; setTimeout(() => this.onopen && this.onopen(), 0); } send() {} close() {} };
    } });
  const w = dom.window;
  w.eval(js);
  await sleep(50);
  return { w, calls, $: (id) => w.document.getElementById(id), ev: (e) => w.__ws.onmessage({ data: JSON.stringify(e) }) };
}

const cp0 = fx.snapshot.current_player, v0 = fx.snapshot.room.version;
const mkBid = (version, amount, who, whoId) => ({ type: "BID_ACCEPTED", room_version: version, lot_id: cp0.lot_id, player_id: cp0.player_id,
  player_name: cp0.name, highest_bid: amount, leading_franchise_id: whoId, leading_franchise_name: who, next_bid_amount: amount + 10,
  timer_deadline: new Date(Date.now() + 20000).toISOString(), bid_sequence: version - v0, server_time: new Date().toISOString(),
  event: { id: 9000 + version, kind: "BID", message: `${who} bid ${amount}.`, created_at: new Date().toISOString() } });
const other = fx.snapshot.franchises.find((f) => f.id !== fx.me.franchise.id);
const me = fx.me.franchise;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("compact event patches the page in place without rebuilding it", async () => {
  const { w, $, ev, calls } = await boot();
  const grid = $("franchises"), firstCard = grid.querySelector("details");
  firstCard.open = true;
  let rebuilds = 0;
  const desc = Object.getOwnPropertyDescriptor(w.Element.prototype, "innerHTML");
  Object.defineProperty(grid, "innerHTML", { get() { return desc.get.call(this) }, set(v) { rebuilds++; desc.set.call(this, v) } });
  const before = calls.length;
  ev(mkBid(v0 + 1, 100, other.name, other.id));
  assert.strictEqual(rebuilds, 0, "franchise cards were rebuilt");
  assert.strictEqual(grid.querySelector("details"), firstCard, "card node replaced");
  assert.ok(firstCard.open, "expanded squad card collapsed");
  assert.strictEqual($("current-bid").textContent, "100");
  assert.ok($("leading").textContent.includes(other.name));
  assert.strictEqual($("next-bid").textContent, "110");
  assert.strictEqual($("bid-button").disabled, false);
  assert.ok(grid.querySelector(`details[data-franchise="${other.id}"]`).classList.contains("leading-team"));
  assert.ok($("activity").textContent.includes(`${other.name} bid 100.`));
  assert.strictEqual(calls.length, before, "a compact event must not trigger any network request");
  // when I take the lead, my button is disabled until someone else bids
  ev(mkBid(v0 + 2, 110, me.name, me.id));
  assert.strictEqual($("bid-button").disabled, true);
  ev(mkBid(v0 + 3, 120, other.name, other.id));
  assert.strictEqual($("bid-button").disabled, false);
});

test("H: duplicate / older events are ignored and never revert newer state", async () => {
  const { $, ev, calls } = await boot();
  ev(mkBid(v0 + 1, 100, other.name, other.id));
  ev(mkBid(v0 + 2, 110, me.name, me.id));
  ev(mkBid(v0 + 2, 110, me.name, me.id));                    // duplicate
  ev(mkBid(v0 + 1, 100, other.name, other.id));              // older, delivered late
  assert.strictEqual($("current-bid").textContent, "110");
  assert.ok($("leading").textContent.includes(me.name));
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/snapshot")).length, 1, "only the initial snapshot fetch");
  // an older full snapshot must not overwrite either
  ev({ type: "snapshot", data: fx.snapshot });
  assert.strictEqual($("current-bid").textContent, "110");
});

test("H: out-of-order delivery (v+2 before v+1) triggers one recovery and ends on the newest state", async () => {
  const { w, $, ev, calls } = await boot();
  const newest = JSON.parse(JSON.stringify(fx.snapshot));
  newest.room.version = v0 + 2;
  newest.current_player.current_bid = 110; newest.current_player.highest_franchise_id = me.id;
  newest.current_player.highest_franchise = me.name; newest.current_player.next_bid = 120;
  w.__nextSnapshot = newest;
  ev(mkBid(v0 + 2, 110, me.name, me.id));                    // arrives first: gap -> resync
  ev(mkBid(v0 + 2, 110, me.name, me.id));                    // coalesced, no second fetch
  ev(mkBid(v0 + 1, 100, other.name, other.id));              // the late one
  await sleep(100);
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/snapshot")).length, 2);
  assert.strictEqual($("current-bid").textContent, "110");
  assert.strictEqual($("next-bid").textContent, "120");
  ev(mkBid(v0 + 3, 120, other.name, other.id));              // v+3 applies cleanly => local version is v+2
  assert.strictEqual($("current-bid").textContent, "120");
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/snapshot")).length, 2, "no further recovery needed");
  ev(mkBid(v0 + 1, 100, other.name, other.id));              // still ignored afterwards
  assert.strictEqual($("current-bid").textContent, "120");
});

test("I: a missed event (version gap) is repaired with a full snapshot", async () => {
  const { w, $, ev, calls } = await boot();
  const newest = JSON.parse(JSON.stringify(fx.snapshot));
  newest.room.version = v0 + 5; newest.current_player.current_bid = 150; newest.current_player.next_bid = 160;
  newest.current_player.highest_franchise = other.name; newest.current_player.highest_franchise_id = other.id;
  w.__nextSnapshot = newest;
  ev(mkBid(v0 + 5, 150, other.name, other.id));
  await sleep(100);
  assert.strictEqual($("current-bid").textContent, "150");
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/snapshot")).length, 2);
});

test("bid sends the exact displayed amount, the lot id and a request id", async () => {
  let sent;
  const { $, w } = await boot({ bidResponse: (opts) => { sent = opts; return { ok: true, status: 200, json: async () => mkBid(v0 + 1, cp0.next_bid, me.name, me.id) }; } });
  $("bid-button").dispatchEvent(new w.Event("pointerdown"));
  $("bid-button").click();
  await sleep(50);
  const body = JSON.parse(sent.body);
  assert.deepStrictEqual([body.amount, body.lot_id, body.franchise_id], [cp0.next_bid, cp0.lot_id, me.id]);
  assert.ok(/^[A-Za-z0-9]{8,}$/.test(sent.headers["X-Request-Id"]));
  assert.ok($("bid-feedback").textContent.startsWith("Bid accepted"));
  assert.strictEqual($("bid-button").disabled, true);        // I lead now
});

test("price changing between pointerdown and click: the price the user pressed is sent (and rejected as stale)", async () => {
  let sent;
  const { $, w, ev } = await boot({ bidResponse: (opts) => { sent = opts; return { ok: false, status: 409, json: async () => ({ code: "STALE_BID", detail: "x", state: {
    room_version: v0 + 1, room_status: "LIVE", lot_id: cp0.lot_id, highest_bid: cp0.next_bid, leading_franchise_id: other.id, leading_franchise_name: other.name,
    next_bid_amount: cp0.next_bid + 10, timer_deadline: new Date(Date.now() + 20000).toISOString(), server_time: new Date().toISOString() } }) }; } });
  $("bid-button").dispatchEvent(new w.Event("pointerdown"));
  ev(mkBid(v0 + 1, cp0.next_bid, other.name, other.id));     // another team bids while the finger is down
  $("bid-button").click();
  await sleep(50);
  assert.strictEqual(JSON.parse(sent.body).amount, cp0.next_bid, "must send the price that was on screen at pointerdown");
});

test("409 rejection updates the display immediately so the user can decide again", async () => {
  const { $ } = await boot({ bidResponse: () => ({ ok: false, status: 409, json: async () => ({ code: "STALE_BID", accepted: false, detail: "x", state: {
    room_version: v0 + 1, room_status: "LIVE", lot_id: cp0.lot_id, highest_bid: 100, leading_franchise_id: other.id, leading_franchise_name: other.name,
    next_bid_amount: 110, timer_deadline: new Date(Date.now() + 20000).toISOString(), server_time: new Date().toISOString() } }) }) });
  $("bid-button").click();
  await sleep(50);
  assert.strictEqual($("next-bid").textContent, "110");
  assert.strictEqual($("current-bid").textContent, "100");
  assert.ok($("leading").textContent.includes(other.name));
  assert.ok($("bid-feedback").textContent.includes("110"));
  assert.strictEqual($("bid-button").disabled, false);       // free to bid 110 deliberately
});

test("network failure is reported as unconfirmed and re-syncs instead of assuming success", async () => {
  const { $, calls } = await boot({ bidResponse: () => { throw new Error("offline"); } });
  $("bid-button").click();
  await sleep(100);
  assert.ok($("bid-feedback").textContent.includes("not confirmed"));
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/snapshot")).length, 2);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log("ok   -", name); } catch (e) { failed++; console.log("FAIL -", name, "\n      ", e.message); }
  }
  process.exit(failed ? 1 : 0);
})();
