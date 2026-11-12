const $ = (id) => document.getElementById(id);
let roomCode=null, token=null, isHost=false, myFranchise=null, ws=null, state=null, pool=[], timerInterval=null, lastSnapshot=null;
let hasInitialSnapshot=false, lastCelebrationEventId=null, audioContext=null;
let reconnectDelay=1000, reconnectTimer=null, heartbeatTimer=null;
// Real-time sync state. `state` is the single source of truth in the browser; full snapshots replace
// it, compact bid events patch it. Every message is ordered by the server's room version.
let bidInFlight=false, armedBid=null, resyncing=false, bidFieldsVersion=0;
window.__bidStats=[];
// ---- session: survives refreshes, closed tabs and dropped connections ---------------------------
// Only an explicit sign-out, or the server answering 401 (password reset / bad token), ends a session.
// A network failure never does.
const SESSION_KEY="smat.session";
function saveSession(){try{localStorage.setItem(SESSION_KEY,JSON.stringify({room:roomCode,token}))}catch(e){}}
function loadSession(){try{const s=JSON.parse(localStorage.getItem(SESSION_KEY)||"null");return s&&s.room&&s.token?s:null}catch(e){return null}}
function clearSession(){try{localStorage.removeItem(SESSION_KEY)}catch(e){}}
const params=new URLSearchParams(location.search);
roomCode=params.get("room"); token=params.get("token");
if(roomCode&&token){saveSession();history.replaceState({},"","/")}      // old invite links still work, then tidy the URL
else{const s=loadSession();if(s){roomCode=s.room;token=s.token}else{roomCode=null;token=null}}
class AuthError extends Error{}
const money=(n)=>Number(n||0).toLocaleString("en-IN");
// ---- design helpers: franchise monogram badges, role labels, activity icons ------------------
const PALETTE=[["#2563eb","#1e3a8a","#fff"],["#fcd34d","#d97706","#2b1a00"],["#ef4444","#991b1b","#fff"],["#8b5cf6","#4c1d95","#fff"],
 ["#0ea5e9","#0369a1","#fff"],["#ec4899","#9d174d","#fff"],["#fb923c","#c2410c","#fff"],["#4c63b6","#1b2557","#fff"],["#be185d","#6b0f3a","#fff"],["#10b981","#047857","#fff"]];
let franchiseOrder=[];
function initials(name){const w=String(name||"?").trim().split(/\s+/).filter(Boolean);return(w.length>1?w[0][0]+w[1][0]:String(name||"?").trim().slice(0,3)).toUpperCase()}
function badgeHTML(name,id){
 let i=franchiseOrder.indexOf(Number(id));if(i<0){i=0;for(const c of String(name))i=(i*31+c.charCodeAt(0))>>>0}
 const [a,b,f]=PALETTE[i%PALETTE.length];
 return `<span class="badge" style="--b1:${a};--b2:${b};--bf:${f}">${esc(initials(name))}</span>`;
}
const ROLE_LABELS={BAT:"Batter",BOWL:"Bowler",AR:"All-Rounder",WK:"Wicket-Keeper"};
const roleLabel=(r)=>ROLE_LABELS[String(r||"").toUpperCase()]||String(r||"Player");
const ICON=(p)=>`<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
const ICONS={
 BID:ICON('<path d="m14 13-8.5 8.5a2.12 2.12 0 0 1-3-3L11 10"/><path d="m16 16 6-6"/><path d="m8 8 6-6"/><path d="m9 7 8 8"/><path d="m21 11-8-8"/>'),
 SOLD:ICON('<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>'),
 UNSOLD:ICON('<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>'),
 UNDO:ICON('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'),
 REDO:ICON('<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>'),
 PAUSE:ICON('<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'),
 PLAY:ICON('<path d="m7 4 13 8-13 8z"/>'),
 INFO:ICON('<circle cx="12" cy="12" r="4"/>')
};
const EVENT_ICON={BID:"BID",SOLD:"SOLD",UNSOLD:"UNSOLD",BID_UNDONE:"UNDO",PLAYER_UNDONE:"UNDO",BID_REDONE:"REDO",PLAYER_REDONE:"REDO",PAUSED:"PAUSE",RECOVERED_PAUSED:"PAUSE",RESUMED:"PLAY",OUT:"UNDO",IN:"REDO",ALL_OUT:"PAUSE"};
function activityHTML(e){
 const k=String(e.kind||"").toLowerCase();
 return `<div class="activity-item ev-${k}">${ICONS[EVENT_ICON[e.kind]||"INFO"]}<span>${esc(e.message)}</span></div>`;
}
function setConnection(text,st){const c=$("connection");if(!c)return;c.dataset.state=st||"off";$("connection-text").textContent=text}
function auth(){return {"Authorization":"Bearer "+token,"Content-Type":"application/json"}}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...auth(),...(opts.headers||{})}});let d=await r.json().catch(()=>({}));if(!r.ok){const m=typeof d.detail==="string"?d.detail:"Request failed";throw (r.status===401?new AuthError(m):new Error(m))}return d}
function show(id){$(id).classList.remove("hidden")} function hide(id){$(id).classList.add("hidden")}
function message(text){$("bid-feedback").textContent=text}
async function init(){
 if(!roomCode||!token){show("landing");hide("room");return}
 hide("landing");show("room");
 $("logout-btn").classList.remove("hidden");
 syncClock(5);
 let me,snap;
 for(let attempt=0;;attempt++){
   try{me=await api(`/api/rooms/${roomCode}/me`);snap=await api(`/api/rooms/${roomCode}/snapshot`);break}
   catch(e){
     if(e instanceof AuthError||/not found/i.test(e.message)){endSession("Your session is no longer valid. Please sign in again.");return}
     setConnection("Reconnecting…","wait");              // offline or server restarting: keep the session, keep trying
     await new Promise(r=>setTimeout(r,Math.min(1000*(attempt+1),8000)));
   }
 }
 try{
   isHost=me.is_host;
   myFranchise=me.franchise;
   paintRoleLabel();
   if(isHost){
    if(snap.room.status==="SETUP"){show("host-setup");await loadPool()}
    else {show("live-area");show("admin-controls")}
   }
   else if(myFranchise){show("live-area")}
   else {show("live-area");$("bid-button").disabled=true;message("Sign in with your franchise to enable bidding.")}
   await render(snap); connect();
 }catch(e){console.error(e);setConnection("Reconnecting…","wait");setTimeout(()=>init(),3000)}
}
function endSession(msg){
 clearSession();token=null;roomCode=null;isHost=false;myFranchise=null;
 clearTimeout(reconnectTimer);reconnectTimer=null;clearInterval(heartbeatTimer);
 if(ws){const w=ws;ws=null;w.onclose=null;try{w.close()}catch(e){}}
 state=null;hasInitialSnapshot=false;
 hide("room");show("landing");$("logout-btn").classList.add("hidden");$("me-chip").classList.add("hidden");$("role-label").classList.add("hidden");
 setConnection("Not connected","off");
 const er=$("login-error");if(msg){er.textContent=msg;er.classList.remove("hidden")}else er.classList.add("hidden");
}
$("logout-btn").onclick=()=>{if(confirm("Sign out of this auction on this device?")){endSession("");location.href="/"}};
function paintRoleLabel(){
 const el=$("role-label");if(!el)return;
 el.textContent=isHost?(myFranchise?"Host":"Administrator"):(myFranchise?myFranchise.name:"Guest");
 el.classList.toggle("hidden",!isHost);   // franchise owners see their team chip instead
}
async function render(s){
 // Concurrent bid responses can arrive out of order. Never let an older
 // committed room version overwrite a newer snapshot already rendered.
 if(state && Number(state.room?.version||0)>Number(s.room?.version||0))return;
 state=s;lastSnapshot=s;bidFieldsVersion=0;noteSnapshotTime(s);
 franchiseOrder=(s.franchises||[]).map(f=>Number(f.id));
 if(isHost){show("access-panel");const key=franchiseOrder.join(",")+"|"+(s.room.host_franchise_id||"");if(key!==render._accessKey){render._accessKey=key;if($("access-details").open)loadCredentials()}}
 if(isHost){
  if(s.room.host_franchise_id){const hf=(s.franchises||[]).find(f=>Number(f.id)===Number(s.room.host_franchise_id));if(hf)myFranchise={id:hf.id,name:hf.name};}
  syncHostFranchisePickers(s);
 }
 // The server sends the latest events in descending ID order. Establish a
 // baseline on the first snapshot so joining/reloading does not replay old
 // auction results; on later snapshots, celebrate each newly-created result.
 const snapshotEvents=Array.isArray(s.events)?s.events:[];
 const newestEventId=snapshotEvents.reduce((max,e)=>Math.max(max,Number(e.id)||0),0);
 if(!hasInitialSnapshot){
  lastCelebrationEventId=newestEventId;
  hasInitialSnapshot=true;
 }else{
  const newResults=snapshotEvents
   .filter(e=>(Number(e.id)||0)>lastCelebrationEventId && (e.kind==="SOLD"||e.kind==="UNSOLD"))
   .sort((a,b)=>(Number(a.id)||0)-(Number(b.id)||0));
  if(newResults.length&&s.room.status==="BETWEEN_LOTS")celebrate(newResults[newResults.length-1]);
  lastCelebrationEventId=Math.max(lastCelebrationEventId||0,newestEventId);
 }
 if(ws&&ws.readyState===1)setConnection("Connected","on");else setConnection("Connecting…","wait");
 const expandedFranchises=new Set([...document.querySelectorAll("#franchises details[open]")].map(el=>String(el.dataset.franchise)));
 const others=s.franchises.filter(f=>!(myFranchise&&Number(myFranchise.id)===Number(f.id)));
 $("franchises").innerHTML=others.map(f=>{
  const squad=f.squad||[];
  const expanded=expandedFranchises.has(String(f.id));
  const pct=Math.min(100,Math.round(100*(f.squad_count||0)/Math.max(1,s.room.max_squad)));
  const leads=s.current_player&&s.current_player.highest_franchise_id===f.id;
  return `<details data-franchise="${f.id}" class="franchise ${leads?"leading-team":""}" ${expanded?"open":""}>
   <summary>${badgeHTML(f.name,f.id)}<div class="t-name">${esc(f.name)}<span class="chip lead">Leading</span></div><div class="t-purse"><b>${money(f.purse_remaining)}</b><small>Purse</small></div><div class="t-squad"><b>${f.squad_count}/${s.room.max_squad}</b><div class="bar"><i style="width:${pct}%"></i></div></div></summary>
   <div class="franchise-squad">${squad.length?`<table><thead><tr><th>Player</th><th>Role</th><th>Price</th></tr></thead><tbody>${squad.map(x=>`<tr><td>${esc(x.player)}</td><td>${esc(roleLabel(x.role))}</td><td>${money(x.price)}</td></tr>`).join("")}</tbody></table>`:`<span class="muted">No players purchased yet.</span>`}</div>
  </details>`;
 }).join("");
 $("franchises-title").classList.toggle("hidden",!(myFranchise&&others.length));
 $("tab-count-f").textContent=s.franchises.length;
 paintMyTeam(s);refreshPoolIfNeeded(s);
 $("activity").innerHTML=s.events.map(activityHTML).join("")||'<p class="muted">No activity yet.</p>';
 paintMeChip(s);
 if(s.room.status === "COMPLETED"){
  hide("live-area");hide("host-setup");hide("admin-controls");show("final-results");
  renderFinalResults(s);
  clearInterval(timerInterval);
  syncDock();
  return;
 }
 hide("final-results");
 const betweenLots=!s.current_player&&s.room.status==="BETWEEN_LOTS";
 const resultEvent=betweenLots?(s.events||[]).find(e=>e.kind==="SOLD"||e.kind==="UNSOLD"):null;
 $("queue-label").textContent=(s.queue_remaining||0)+" in queue"+(s.next_up?" · Next: "+s.next_up.name:"");
 if(resultEvent){
  // SOLD / UNSOLD replaces the bidding area; the server opens the next player only after this has been shown.
  clearInterval(timerInterval);
  showResult(resultEvent);
  updateBidButton();
 }else if(s.current_player){
  const p=s.current_player;show("live-area");hideResult();
  $("player-name").textContent=p.name;paintRole(p.role);$("player-team").textContent=p.state_team||"";{const ck=$("player-clock"),extra=Math.round((Number(p.timer_seconds)||0)-(Number(s.room.timer_seconds)||0));ck.textContent=`+${extra}s clock`;ck.classList.toggle("hidden",!(extra>0))}
  $("round-label").textContent="ROUND "+p.round_no;$("base-price").textContent=money(p.base_price);$("current-bid").textContent=money(p.current_bid||p.base_price);
  paintBid();
  startLocalTimer(p.ends_at,p.remaining_ms);
 }else{
  hideResult();
  $("player-name").textContent=s.room.status==="ROUND_COMPLETE"?"Round complete":"Waiting for next player";
  paintRole("");$("player-team").textContent="Players remaining: "+s.queue_remaining;$("timer").textContent="—";
  setTimerState("idle");$("timer-fill").style.width="0%";
  $("base-price").textContent="—";$("current-bid").textContent="—";paintLeader(null);paintOut(null);
  $("next-bid").textContent="—";
  updateBidButton();
 }
 if(isHost){
  show("admin-controls");show("host-bar");renderUnsold(s.unsold);
  const status=s.room.status;
  paintHostButtons(s);
  const warning=$("unsold-warning");
  if(warning)warning.textContent=`${s.queue_remaining||0} unseen players remain in the queue. Starting this unsold-only round will skip them and auction only the players you select. The current player, if any, will be settled first.`;
  const nominate=$("nominate");
  if(nominate)nominate.disabled=!(s.unsold||[]).length || status==="COMPLETED";
  const livePicker=$("host-franchise-live");
  if(livePicker){livePicker.innerHTML=hostFranchiseOptions(s);livePicker.value=s.room.host_franchise_id||"";}
 }
 syncDock();
}
// ---- clock sync: every device counts down against the SERVER's clock ---------------------------
// NTP-style: several round trips to /api/time, keep the lowest-latency sample (offset = server time minus
// the midpoint of the request), then blend into the running estimate. Repeats while the page is open.
let clockOffset=0,clockSynced=false;
const serverNow=()=>Date.now()+clockOffset;
async function syncClock(samples=5){
 let best=null;
 for(let i=0;i<samples;i++){
  try{
   const w0=Date.now(),t0=performance.now();
   const r=await fetch("/api/time",{cache:"no-store"});const d=await r.json();
   const rtt=performance.now()-t0;
   if(!best||rtt<best.rtt)best={rtt,offset:d.t-(w0+rtt/2)};
  }catch(e){break}
  await new Promise(r=>setTimeout(r,i<samples-1?80:0));
 }
 if(!best||best.rtt>2500)return;
 clockOffset=clockSynced?clockOffset*0.4+best.offset*0.6:best.offset;
 clockSynced=true;
 window.__clock={offset:Math.round(clockOffset),rtt:Math.round(best.rtt)};
}
function noteSnapshotTime(s){   // rough prior until the first real sync completes
 if(!clockSynced&&s&&s.server_time){const t=Date.parse(s.server_time);if(t)clockOffset=t-Date.now()}
}
setInterval(()=>{if(roomCode&&!document.hidden)syncClock(3)},15000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&roomCode)syncClock(4)});
window.addEventListener("online",()=>{if(roomCode)syncClock(4)});

// ---- bid box: who is leading, right next to the price ---------------------------------------------
function paintLeader(p){
 const box=$("leading"),b=$("leader-badge"),name=$("leader-name"),lab=$("leader-label"),you=$("leader-you");
 if(!p||!p.highest_franchise){
  box.className="leading";b.innerHTML="";name.textContent="No bids yet";lab.textContent="Opening bid";you.classList.add("hidden");return;
 }
 const mine=!!(myFranchise&&Number(p.highest_franchise_id)===Number(myFranchise.id));
 box.className="leading has-leader"+(mine?" is-me":"");
 b.innerHTML=badgeHTML(p.highest_franchise,p.highest_franchise_id);
 name.textContent=p.highest_franchise;lab.textContent="Highest bidder";you.classList.toggle("hidden",!mine);
}

// ---- your team card (top of the Franchises tab) ----------------------------------------------------
const ROLE_ORDER=["BAT","BOWL","AR","WK"];
function paintMyTeam(s){
 const el=$("my-squad");if(!el)return;
 const f=myFranchise&&(s.franchises||[]).find(x=>Number(x.id)===Number(myFranchise.id));
 if(!f){el.innerHTML=isHost?'<p class="muted admin-note">Administrator view. Pick a franchise before the auction starts if you want to bid as well.</p>':"";return}
 const squad=[...(f.squad||[])].sort((a,b)=>b.price-a.price);
 const max=Math.max(1,s.room.max_squad),min=s.room.min_squad||0;
 const pct=Math.min(100,Math.round(100*squad.length/max)),minPct=Math.min(100,Math.round(100*min/max));
 const counts={};squad.forEach(x=>{const r=String(x.role||"").toUpperCase();counts[r]=(counts[r]||0)+1});
 const roleChips=ROLE_ORDER.map(r=>`<span class="rc" data-role="${r}"><b>${counts[r]||0}</b>${esc(roleLabel(r).replace("Wicket-Keeper","Keeper"))}</span>`).join("");
 const need=Math.max(0,min-squad.length);
 const leads=s.current_player&&Number(s.current_player.highest_franchise_id)===Number(f.id);
 const spent=squad.reduce((t,x)=>t+Number(x.price||0),0);
 el.innerHTML=`<div class="team-card${leads?" leading-team":""}">
  <div class="team-top">${badgeHTML(f.name,f.id)}
   <div class="team-id"><span class="eyebrow">Your team</span><h3>${esc(f.name)}</h3></div>
   <span class="chip lead">Leading</span></div>
  <div class="team-stats">
   <div><small>Purse left</small><strong>${money(f.purse_remaining)}</strong></div>
   <div><small>Squad</small><strong>${squad.length}<em>/${max}</em></strong></div>
   <div><small>Spent</small><strong>${money(spent)}</strong></div></div>
  <div class="squad-meter" title="Squad size (marker = minimum ${min})"><i style="width:${pct}%"></i><u style="left:${minPct}%"></u></div>
  <div class="squad-note">${need?`Need ${need} more to reach the minimum of ${min}`:`Minimum squad of ${min} reached`}</div>
  <div class="role-counts">${roleChips}</div>
  <div class="team-players">${squad.length?squad.map(x=>`<div class="tp-row"><span class="role-dot" data-role="${esc(String(x.role||"").toUpperCase())}"></span><div class="tp-name"><b>${esc(x.player)}</b><small>${esc(roleLabel(x.role))}${x.state_team?" · "+esc(x.state_team):""}</small></div><strong>${money(x.price)}</strong></div>`).join(""):'<div class="empty-squad">No players yet. Your first buy appears here.</div>'}</div>
 </div>`;
}

// ---- Franchises / Activity tabs ---------------------------------------------------------------------
function selectSideTab(name){
 document.querySelectorAll(".side-tab").forEach(t=>{const on=t.dataset.side===name;t.classList.toggle("active",on);t.setAttribute("aria-selected",on)});
 for(const k of ["franchises","pool","activity"])(name===k?show:hide)("pane-"+k);
 if(name==="pool")loadAuctionPool();
 try{localStorage.setItem("smat.sideTab",name)}catch(e){}
}
document.querySelectorAll(".side-tab").forEach(t=>t.onclick=()=>selectSideTab(t.dataset.side));
try{const t=localStorage.getItem("smat.sideTab");if(t==="activity"||t==="franchises"||t==="pool")selectSideTab(t)}catch(e){}

function paintRole(r){const el=$("player-role");el.textContent=r?roleLabel(r):"Player";el.dataset.role=String(r||"").toUpperCase()}
function paintMeChip(s){
 const el=$("me-chip");if(!el)return;
 const f=myFranchise&&(s.franchises||[]).find(x=>Number(x.id)===Number(myFranchise.id));
 if(!f){el.classList.add("hidden");return}
 el.innerHTML=`${badgeHTML(f.name,f.id)}<span><b>${esc(f.name)}</b><small>Purse ${money(f.purse_remaining)} · ${f.squad_count}/${s.room.max_squad}</small></span>`;
 el.classList.remove("hidden");
}
function setTimerState(st){
 $("timer-box").dataset.state=st;
 const u=$("timer-box").querySelector(".unit");if(u)u.textContent=st==="paused"?"PAUSED":"SEC";
 $("player-panel").dataset.urgent=st==="urgent"?"1":"0";
}
// SOLD / UNSOLD stage (inside the player card). Driven by room state, so every device shows it for
// exactly as long as the server holds the next player back.
function showResult(e){
 const sold=e.kind==="SOLD",data=e.data||{};
 const player=data.player_name||(e.message||"").split(sold?" sold to ":" went unsold")[0]||"Player";
 const stage=$("result-stage");
 stage.classList.toggle("unsold",!sold);
 $("result-tag").textContent=sold?"SOLD":"UNSOLD";
 $("result-player").textContent=player;
 $("result-price").textContent=sold?money(data.price):"No bids received";
 $("result-buyer").innerHTML=sold&&data.franchise_name?`${badgeHTML(data.franchise_name,data.franchise_id)}<span>${esc(data.franchise_name)}</span>`:"";
 show("result-stage");document.body.classList.add("has-result");
 setTimerState("idle");$("timer-fill").style.width="0%";
}
function hideResult(){hide("result-stage");document.body.classList.remove("has-result");$("result-confetti").innerHTML=""}
function paintHostButtons(s){
 const status=s.room.status, history=s.history||{};
 const controls={pause:status!=="LIVE",resume:status!=="PAUSED",
  "undo-bid":!history.can_undo_bid,"redo-bid":!history.can_redo_bid,
  "undo-sale":!history.can_undo_sale,"redo-sale":!history.can_redo_sale,
  finish:status==="COMPLETED"};
 document.querySelectorAll("[data-action]").forEach(b=>b.disabled=!!controls[b.dataset.action]);
 const pill=$("room-status");
 if(pill){pill.dataset.status=status;pill.textContent=({LIVE:"Live",PAUSED:"Paused",BETWEEN_LOTS:"Between players",ROUND_COMPLETE:"Round complete",COMPLETED:"Completed",SETUP:"Setup"})[status]||status}
}
function hostFranchiseOptions(s){
 return `<option value="">Host only — not bidding</option>`+(s.franchises||[]).map(f=>`<option value="${f.id}">${esc(f.name)}</option>`).join("");
}
function syncHostFranchisePickers(s){
 const sel=String(s.room.host_franchise_id||"");
 const live=$("host-franchise-live");
 if(live){const k="L"+(s.franchises||[]).map(f=>f.id+f.name).join(",");if(live.dataset.k!==k){live.dataset.k=k;live.innerHTML=hostFranchiseOptions(s)}live.value=sel}
 const box=$("host-franchise-chips");if(!box)return;
 const opts=[{id:"",name:"Host only"},...(s.franchises||[]).map(f=>({id:String(f.id),name:f.name}))];
 const k=opts.map(o=>o.id+o.name).join("|")+"#"+sel+"#"+s.room.status;
 if(box.dataset.k===k)return;box.dataset.k=k;
 const locked=s.room.status!=="SETUP";
 box.innerHTML=opts.map(o=>`<button type="button" role="radio" aria-checked="${o.id===sel}" class="fr-chip${o.id===sel?" on":""}" data-fid="${o.id}" ${locked?"disabled":""}>${esc(o.name)}</button>`).join("");
 box.querySelectorAll(".fr-chip").forEach(b=>b.onclick=()=>updateHostFranchise(b.dataset.fid));
}
function renderFinalResults(s){
 const franchises=s.franchises||[];
 const totalPlayers=franchises.reduce((n,f)=>n+(f.squad_count||0),0);
 const totalSpent=franchises.reduce((n,f)=>n+Math.max(0,(s.room.purse||0)-(f.purse_remaining||0)),0);
 $("final-summary").innerHTML=`
  <div class="final-stat panel"><span>FRANCHISES</span><strong>${franchises.length}</strong></div>
  <div class="final-stat panel"><span>PLAYERS SOLD</span><strong>${totalPlayers}</strong></div>
  <div class="final-stat panel"><span>TOTAL SPENT</span><strong>${money(totalSpent)}</strong></div>`;
 const isMine=f=>!!(myFranchise&&Number(myFranchise.id)===Number(f.id));
 const ordered=[...franchises].sort((a,b)=>isMine(b)-isMine(a));
 $("final-franchises").innerHTML=ordered.map(f=>{
  const squad=f.squad||[];
  const spent=Math.max(0,(s.room.purse||0)-(f.purse_remaining||0));
  return `<article class="final-team panel${isMine(f)?" mine":""}">
   <div class="final-team-head"><div><div class="eyebrow">FINAL SQUAD${isMine(f)?'<span class="you-tag">Your team</span>':""}</div><h3>${esc(f.name)}</h3></div><div class="final-team-count">${squad.length} / ${s.room.max_squad}</div></div>
   <div class="final-team-metrics"><div><span>Spent</span><strong>${money(spent)}</strong></div><div><span>Remaining purse</span><strong>${money(f.purse_remaining)}</strong></div></div>
   ${squad.length?`<div class="table-wrap"><table class="final-squad-table"><thead><tr><th>#</th><th>Player</th><th class="col-state">State team</th><th class="col-state">Role</th><th>Price</th></tr></thead><tbody>${squad.map((p,i)=>`<tr><td>${i+1}</td><td>${esc(p.player)}</td><td>${esc(p.state_team||"—")}</td><td>${esc(p.role||"—")}</td><td>${money(p.price)}</td></tr>`).join("")}</tbody></table></div>`:`<p class="muted">No players purchased.</p>`}
  </article>`;
 }).join("");
}

// ---- celebration: cannons from both corners plus a rain of paper, across the whole page ----------------
const CONFETTI_COLORS=["#fbbf24","#f97316","#fde68a","#34d399","#60a5fa","#f472b6","#ffffff","#a78bfa","#ef4444"];
function confettiPiece(layer,kind,i){
 const el=document.createElement("i");
 const w=kind==="strip"?6+Math.random()*4:10+Math.random()*10,h=kind==="strip"?26+Math.random()*22:kind==="dot"?w:14+Math.random()*16;
 el.className="cf cf-"+kind;
 el.style.cssText=`width:${w}px;height:${h}px;background:${CONFETTI_COLORS[i%CONFETTI_COLORS.length]}`;
 layer.appendChild(el);return el;
}
function celebrate(event){
 const sold=event.kind==="SOLD",layer=$("result-confetti");layer.innerHTML="";
 const reduce=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;
 const W=window.innerWidth,H=window.innerHeight,kinds=["rect","rect","strip","dot"];
 const fly=(el,frames,dur,delay)=>{if(el.animate)el.animate(frames,{duration:dur,delay,easing:"linear",fill:"forwards"});};
 if(sold){
  const cannon=reduce?24:W<600?46:80;
  // Two cannons fire up and inwards, then gravity pulls the paper down.
  for(const side of [0,1]){
   for(let i=0;i<cannon;i++){
    const el=confettiPiece(layer,kinds[i%4],i+side*3);
    const x0=side?W+10:-10,y0=H+10,dir=side?-1:1;
    const vx=dir*(180+Math.random()*(W*0.7)),vy=-(H*0.55+Math.random()*H*0.75),g=H*1.55+Math.random()*H*0.3;
    const rot=(Math.random()<.5?-1:1)*(360+Math.random()*900),T=2.6+Math.random()*1.9;
    const frames=[];for(let k=0;k<=14;k++){const t=k/14*T;frames.push({transform:`translate3d(${x0+vx*t*0.62}px,${y0+vy*t+0.5*g*t*t}px,0) rotate(${rot*k/14}deg) scale(${1-0.15*k/14})`,opacity:k>12?0:1})}
    fly(el,frames,T*1000,Math.random()*450+side*120);
   }
  }
  // A shower from the top after the cannons.
  const rain=reduce?18:W<600?50:110;
  for(let i=0;i<rain;i++){
   const el=confettiPiece(layer,kinds[i%4],i),x=Math.random()*W,sway=(Math.random()-.5)*220,T=3+Math.random()*2.2;
   fly(el,[{transform:`translate3d(${x}px,-40px,0) rotate(0deg)`,opacity:1},{transform:`translate3d(${x+sway}px,${H*.55}px,0) rotate(${300+Math.random()*500}deg)`,opacity:1,offset:.55},{transform:`translate3d(${x-sway/2}px,${H+40}px,0) rotate(${700+Math.random()*700}deg)`,opacity:.9}],T*1000,350+Math.random()*1300);
  }
 }else{
  for(let i=0;i<(reduce?8:26);i++){   // unsold: a few slow grey scraps, no party
   const el=confettiPiece(layer,"rect",i);el.style.background="#6b7280";el.style.opacity=".6";
   const x=Math.random()*W,T=3.5+Math.random()*1.5;
   fly(el,[{transform:`translate3d(${x}px,-30px,0) rotate(0)`},{transform:`translate3d(${x+(Math.random()-.5)*120}px,${H+30}px,0) rotate(${Math.random()*360}deg)`}],T*1000,Math.random()*700);
  }
 }
 playCelebrationSound(sold);
}

// ---- sound: bid pings, countdown ticks, an urgent buzzer at zero, and a mute switch --------------------
let soundOn=true;try{soundOn=localStorage.getItem("smat.sound")!=="off"}catch(e){}
function paintSoundBtn(){const b=$("sound-btn");if(!b)return;b.classList.toggle("off",!soundOn);b.setAttribute("aria-label",soundOn?"Sound on":"Sound off");b.title=soundOn?"Sound on (tap to mute)":"Sound off (tap to unmute)"}
paintSoundBtn();
$("sound-btn").onclick=()=>{soundOn=!soundOn;try{localStorage.setItem("smat.sound",soundOn?"on":"off")}catch(e){}paintSoundBtn();prepareCelebrationAudio();if(soundOn)blip(880,0,.09,"sine",.12)};
function audioReady(){
 if(!soundOn||document.hidden)return null;
 try{
  const AC=window.AudioContext||window.webkitAudioContext;if(!AC)return null;
  audioContext=audioContext||new AC();
  if(audioContext.state==="suspended")audioContext.resume();
  return audioContext;
 }catch(e){return null}
}
function blip(freq,offset,dur,type,vol,slideTo){
 const ctx=audioReady();if(!ctx)return;
 const t=ctx.currentTime+offset,o=ctx.createOscillator(),g=ctx.createGain();
 o.type=type||"sine";o.frequency.setValueAtTime(freq,t);if(slideTo)o.frequency.exponentialRampToValueAtTime(slideTo,t+dur);
 g.gain.setValueAtTime(0.0001,t);g.gain.exponentialRampToValueAtTime(vol,t+0.012);g.gain.exponentialRampToValueAtTime(0.0001,t+dur);
 o.connect(g);g.connect(ctx.destination);o.start(t);o.stop(t+dur+0.03);
}
let lastBidPing=0;
function playBidSound(){            // a bright two-note "ka-ching" for every accepted bid
 const n=performance.now();if(n-lastBidPing<60)return;lastBidPing=n;
 blip(988,0,.11,"triangle",.14);blip(1319,.07,.16,"triangle",.13);
}
function playTick(secLeft){         // soft click each second; higher and louder as it runs out
 const f=secLeft<=3?1480:secLeft<=5?1180:940;
 blip(f,0,.05,"square",secLeft<=3?.07:.045);
}
function playTimeUp(){              // urgent double buzzer at zero
 blip(220,0,.22,"sawtooth",.13,180);blip(220,.26,.34,"sawtooth",.14,150);
}
function prepareCelebrationAudio(){audioReady()}
["pointerdown","keydown","touchstart"].forEach(ev=>document.addEventListener(ev,prepareCelebrationAudio,{passive:true}));
function playCelebrationSound(sold){
 if(sold){   // rising fanfare with a sparkle on top
  [523.25,659.25,783.99,1046.5,1318.5].forEach((f,i)=>blip(f,i*.11,.34,"triangle",.14));
  [1568,2093,2637].forEach((f,i)=>blip(f,.55+i*.09,.18,"sine",.05));
 }else{      // unsold: a clear "wah-wah-wah-waaah" sad trombone and a low thud
  [[392,0],[370,.3],[349,.6],[311,.9]].forEach(([f,t],i)=>blip(f,t,i===3?.9:.26,"sawtooth",.1,i===3?233:f*.97));
  blip(110,0,.3,"sine",.2,70);
 }
}
function startLocalTimer(endsAt,remainingMs){
 clearInterval(timerInterval);
 if(!endsAt){
  // Paused: show the time that will be on the clock when bidding resumes.
  const total=Math.max(1,Number(state?.current_player?.timer_seconds||state?.room?.timer_seconds)||10),sec=Math.max(0,(Number(remainingMs)||0)/1000);
  $("timer").textContent=remainingMs!=null?sec.toFixed(1):"PAUSED";
  $("timer-fill").style.width=Math.min(100,100*sec/total)+"%";
  setTimerState("paused");return}
 const normalizedEndsAt=/([zZ]|[+-]\d{2}:?\d{2})$/.test(endsAt)?endsAt:endsAt+"Z";
 const deadline=new Date(normalizedEndsAt).getTime();
 const total=Math.max(1,Number(state?.current_player?.timer_seconds||state?.room?.timer_seconds)||10);
 let lastWhole=null,buzzed=false;
 const update=()=>{
  const sec=Math.max(0,(deadline-serverNow())/1000);
  const whole=Math.ceil(sec-0.0001);
  if(whole!==lastWhole){
   if(lastWhole!==null&&whole>0&&whole<=Math.min(10,total))playTick(whole);   // never on the first paint
   lastWhole=whole;
  }
  if(sec<=0&&!buzzed&&lastWhole===0){buzzed=true;if(!startedAtZero)playTimeUp()}
  $("timer").textContent=sec.toFixed(1);
  $("timer-fill").style.width=Math.min(100,100*sec/total)+"%";
  setTimerState(sec<=3?"urgent":"live");
 };
 const startedAtZero=(deadline-serverNow())<=0;
 update();timerInterval=setInterval(update,100);
}
// ---- real-time sync -------------------------------------------------------------------------
const roomVersion=()=>Number(state?.room?.version??-1);
function applyIncoming(m){
 if(!m||!m.type)return;
 if(m.type==="snapshot"){
  // Equal or older versions carry nothing new: ignoring them avoids rebuilding the whole page.
  if(state&&Number(m.data?.room?.version??0)<=roomVersion())return;
  return render(m.data);
 }
 if(m.type==="BID_ACCEPTED")return applyBidEvent(m);
}
function patchBidFields(p,e){
 p.current_bid=e.highest_bid;p.highest_franchise_id=e.leading_franchise_id;
 p.highest_franchise=e.leading_franchise_name;p.next_bid=e.next_bid_amount;p.ends_at=e.timer_deadline;
 p.out_ids=(p.out_ids||[]).filter(id=>Number(id)!==Number(e.leading_franchise_id));
}
// Events normally arrive in order, but the bidder's own HTTP confirmation can overtake the WebSocket
// event for the previous bid. Hold early events briefly and apply them in order; only a version that
// is still missing after GAP_WAIT_MS triggers the (expensive) full-snapshot recovery.
const GAP_WAIT_MS=1500, pendingEvents=new Map();let gapTimer=null;
function applyBidEvent(e){
 const v=Number(e.room_version), have=roomVersion();
 if(!state||!state.current_player){requestResync("no-state");return}
 if(v<=have)return;                                   // duplicate or older than what we already show
 if(v!==have+1){pendingEvents.set(v,e);armGapTimer();return}
 if(!applyContiguousBid(e))return;
 for(;;){const nxt=pendingEvents.get(roomVersion()+1);if(!nxt)break;pendingEvents.delete(roomVersion()+1);if(!applyContiguousBid(nxt))return}
 for(const k of [...pendingEvents.keys()])if(k<=roomVersion())pendingEvents.delete(k);
 if(!pendingEvents.size&&gapTimer){clearTimeout(gapTimer);gapTimer=null}
}
function armGapTimer(){
 if(gapTimer)return;
 gapTimer=setTimeout(()=>{gapTimer=null;if(pendingEvents.size){pendingEvents.clear();requestResync("gap")}},GAP_WAIT_MS);
}
function applyContiguousBid(e){
 const p=state.current_player;
 if(p.lot_id!==e.lot_id||state.room.status!=="LIVE"){pendingEvents.clear();requestResync("lot-changed");return false}
 if(Number(e.room_version)>=bidFieldsVersion){patchBidFields(p,e);bidFieldsVersion=Number(e.room_version)}
 state.room.version=Number(e.room_version);
 (state.recent_bids=state.recent_bids||[]).push({franchise:e.leading_franchise_name,amount:e.highest_bid,sequence:e.bid_sequence});
 state.history={...(state.history||{}),can_undo_bid:true,can_redo_bid:false};
 if(e.event){state.events=[e.event,...(state.events||[])].slice(0,30);prependActivity(e.event)}
 paintBid();startLocalTimer(p.ends_at);playBidSound();
 if(isHost)paintHostButtons(state);
 return true;
}
function applyRejectionState(st){
 // A 409 carries the newest bid state. Show it immediately; the in-order WebSocket events that
 // follow fill in the rest. If they never arrive (socket down) a resync fetches a full snapshot.
 const v=Number(st.room_version);
 if(!state||!state.current_player||v<=roomVersion())return;
 const p=state.current_player;
 if(st.lot_id!==p.lot_id||st.room_status!==state.room.status){requestResync("rejection");return}
 if(v>=bidFieldsVersion){
  patchBidFields(p,{highest_bid:st.highest_bid,leading_franchise_id:st.leading_franchise_id,
   leading_franchise_name:st.leading_franchise_name,next_bid_amount:st.next_bid_amount,timer_deadline:st.timer_deadline});
  bidFieldsVersion=v;
  paintBid();startLocalTimer(p.ends_at);
 }
 setTimeout(()=>{if(roomVersion()<v)requestResync("hint-not-confirmed")},3000);
}
let lastResyncAt=0, resyncQueued=false;
async function requestResync(reason,attempt=0){
 if(attempt===0){
  if(resyncing||resyncQueued)return;                    // one recovery at a time; later triggers coalesce
  const wait=lastResyncAt+3000-Date.now();              // at most one full snapshot per 3 s per browser
  if(wait>0){resyncQueued=true;setTimeout(()=>{resyncQueued=false;requestResync(reason)},wait);return}
 }
 resyncing=true;lastResyncAt=Date.now();
 try{
  const s=await api(`/api/rooms/${roomCode}/snapshot`);
  if(!state||Number(s.room.version)>roomVersion())await render(s);
  resyncing=false;
 }catch(e){
  console.warn("resync failed ("+reason+")",e);
  if(attempt<5)setTimeout(()=>requestResync(reason,attempt+1),1500*(attempt+1));
  else resyncing=false;
 }
}
function paintBid(){
 const p=state?.current_player;if(!p)return;
 $("current-bid").textContent=money(p.current_bid||p.base_price);
 paintLeader(p);paintOut(p);
 $("next-bid").textContent=money(p.next_bid);
 // Highlight the leader without rebuilding the franchise cards (keeps expanded squads open).
 document.querySelectorAll("#franchises details[data-franchise]").forEach(el=>
  el.classList.toggle("leading-team",Number(el.dataset.franchise)===Number(p.highest_franchise_id)));
 const mt=$("my-squad").querySelector(".team-card");if(mt)mt.classList.toggle("leading-team",!!(myFranchise&&Number(p.highest_franchise_id)===Number(myFranchise.id)));
 updateBidButton();
}
function updateBidButton(){
 const b=$("bid-button"),p=state?.current_player;
 const bidder=!(isHost&&!myFranchise);
 $("bid-dock").classList.toggle("hidden",!bidder);
 if(!bidder){hide("bid-button");syncDock();return}
 show("bid-button");
 const leading=!!(p&&myFranchise&&Number(p.highest_franchise_id)===Number(myFranchise.id));
 const live=!!p&&state?.room?.status==="LIVE";
 b.disabled=!myFranchise||!p||!live||bidInFlight||leading;
 const label=$("bid-label");
 if(label)label.textContent=bidInFlight?"Sending…":leading?"You lead":!myFranchise?"Sign in to bid":live?"Place bid":(state?.room?.status==="PAUSED"?"Paused":"Bidding closed");
 syncDock();
}
// On phones the bid button is pinned to the bottom edge; reserve space for it only when it is shown.
function syncDock(){
 const dock=$("bid-dock");
 document.body.classList.toggle("has-dock",!!dock&&!dock.classList.contains("hidden")&&!$("live-area").classList.contains("hidden"));
}
function prependActivity(e){
 const box=$("activity");if(!box)return;
 const placeholder=box.querySelector("p.muted");if(placeholder)placeholder.remove();
 box.insertAdjacentHTML("afterbegin",activityHTML(e));
 while(box.children.length>30)box.lastElementChild.remove();
}
function connect(){
 if(!roomCode||!token)return;
 if(ws&&(ws.readyState===WebSocket.OPEN||ws.readyState===WebSocket.CONNECTING))return;
 const proto=location.protocol==="https:"?"wss":"ws";
 const socket=new WebSocket(`${proto}://${location.host}/ws/${roomCode}?token=${encodeURIComponent(token)}`);
 ws=socket;
 socket.onopen=()=>{
  reconnectDelay=1000;setConnection("Connected","on");syncClock(3);
  clearInterval(heartbeatTimer);
  heartbeatTimer=setInterval(()=>{if(socket.readyState===WebSocket.OPEN)socket.send("ping")},25000);
 };
 socket.onclose=()=>{
  clearInterval(heartbeatTimer);
  if(ws===socket)ws=null;
  setConnection("Reconnecting…","wait");
  if(token&&roomCode)api(`/api/rooms/${roomCode}/me`).catch(e=>{if(e instanceof AuthError)endSession("Your password was reset. Please sign in again.")});
  if(!reconnectTimer&&token){
   const delay=reconnectDelay;reconnectDelay=Math.min(reconnectDelay*2,15000);
   reconnectTimer=setTimeout(()=>{reconnectTimer=null;connect()},delay);
  }
 };
 socket.onerror=()=>socket.close();
 socket.onmessage=(ev)=>{
  try{applyIncoming(JSON.parse(ev.data))}catch(e){console.warn("Ignored malformed WebSocket message",e)}
 };
}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
document.querySelectorAll(".tab[data-tab]").forEach(t=>t.onclick=()=>{
 document.querySelectorAll(".tab[data-tab]").forEach(x=>{x.classList.toggle("active",x===t);x.setAttribute("aria-selected",x===t)});
 (t.dataset.tab==="join"?show:hide)("login-form");(t.dataset.tab==="create"?show:hide)("create-form");
});
$("create-form").addEventListener("submit",async e=>{
 e.preventDefault();const fd=new FormData(e.target);
 const franchises=fd.get("franchises").split("\n").map(x=>x.trim()).filter(Boolean);
 try{
  const d=await fetch("/api/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
   name:fd.get("name"),franchises,purse:Number(fd.get("purse")),timer_seconds:Number(fd.get("timer_seconds")),
   min_squad:Number(fd.get("min_squad")),max_squad:Number(fd.get("max_squad")),bid_extension:fd.get("bid_extension")==="on"
  })}).then(async r=>{const x=await r.json();if(!r.ok)throw Error(x.detail);return x});
  roomCode=d.room_code;token=d.host_token;isHost=true;saveSession();
  showCredentials(d);hide("landing");await init();
 }catch(err){alert(err.message)}
});
// The amount sent is the price that was on the button when the user pressed it (pointerdown),
// so a price that changes under their finger is rejected as stale instead of being bid blindly.
$("bid-button").addEventListener("pointerdown",()=>{
 const p=state?.current_player;
 armedBid=p?{lot_id:p.lot_id,amount:p.next_bid,at:Date.now()}:null;
});
$("bid-button").onclick=async()=>{
 const p=state?.current_player;
 if(!p||$("bid-button").disabled||bidInFlight||!myFranchise)return;
 const armed=armedBid&&armedBid.lot_id===p.lot_id&&Date.now()-armedBid.at<5000?armedBid:null;
 armedBid=null;
 const amount=armed?armed.amount:p.next_bid;
 const requestId=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random().toString(16).slice(2)).replace(/-/g,"").slice(0,16);
 bidInFlight=true;updateBidButton();message("Submitting bid…");
 const t0=performance.now();
 try{
  const r=await fetch(`/api/rooms/${roomCode}/bid`,{method:"POST",headers:{...auth(),"X-Request-Id":requestId},
   body:JSON.stringify({amount,lot_id:p.lot_id,franchise_id:myFranchise.id})});
  const d=await r.json().catch(()=>({}));
  const ms=Math.round(performance.now()-t0);
  window.__bidStats.push({request_id:requestId,amount,status:r.status,code:d.code||null,ack_ms:ms});
  console.info("bid",requestId,r.status,d.code||"accepted",ms+" ms");
  if(r.ok){applyIncoming(d);message(`Bid accepted: ${money(d.highest_bid)} · ${ms} ms`)}
  else{
   if(d.state)applyRejectionState(d.state);
   if(r.status===401){endSession("Your password was reset. Please sign in again.");return}
   message(rejectionText(d)+` [${r.status} ${d.code||"error"}${d.request_id?" "+d.request_id:""}]`);
   // The outcome of a failed request may be unknown (e.g. 503): confirm against the server.
   if(r.status>=500)requestResync("bid-error");
  }
 }catch(e){
  window.__bidStats.push({request_id:requestId,amount,status:0,code:"NETWORK",ack_ms:Math.round(performance.now()-t0)});
  message("Could not reach the server — your bid was not confirmed. Checking the latest price…");
  requestResync("bid-network");
 }finally{bidInFlight=false;updateBidButton()}
};
function rejectionText(d){
 const st=d.state||{};
 const lead=st.leading_franchise_name?`${st.leading_franchise_name} leads at ${money(st.highest_bid)}`:"";
 switch(d.code){
  case "STALE_BID":return `Too slow — the price moved. ${lead?lead+". ":""}${st.next_bid_amount?"Next bid is "+money(st.next_bid_amount)+".":""} Bid again if you still want him.`;
  case "ALREADY_HIGHEST_BIDDER":return "You are already the highest bidder.";
  case "INSUFFICIENT_PURSE":return d.detail||"Insufficient purse for this bid.";
  case "PLAYER_ALREADY_SETTLED":return "That player has already been settled.";
  case "TIMER_EXPIRED":return "Time is up for this player.";
  default:return d.detail||"Bid rejected";
 }
}
$("start-auction").onclick=async()=>{if(!confirm("Lock the player pool and start the auction?"))return;try{const d=await api(`/api/rooms/${roomCode}/start`,{method:"POST"});render(d);show("live-area");hide("host-setup");}catch(e){alert(e.message)}};
document.querySelectorAll("[data-action]").forEach(b=>b.onclick=async()=>{const action=b.dataset.action;if(action==="finish"&&!confirm("End the auction?"))return;try{const d=await api(`/api/rooms/${roomCode}/host/${action}`,{method:"POST"});render(d)}catch(e){alert(e.message)}});
async function loadPool(){pool=await api(`/api/rooms/${roomCode}/pool`);renderPool()}
function renderPool(){
 const q=$("pool-search").value.toLowerCase();
 const rows=pool.filter(p=>(p.name+" "+p.state_team+" "+p.role).toLowerCase().includes(q));
 $("pool-table").innerHTML=`<table><thead><tr><th>Include</th><th>Player</th><th class="col-state">State team</th><th class="col-state">Role</th><th>Base</th><th>Importance</th></tr></thead><tbody>${rows.map(p=>`<tr><td><input type="checkbox" data-player="${p.id}" ${p.included?"checked":""}></td><td>${esc(p.name)}<small class="pt-state">${esc(p.role)}${p.state_team?" · "+esc(p.state_team):""}</small></td><td class="col-state">${esc(p.state_team)}</td><td class="col-state">${esc(p.role)}</td><td><input class="small-input" type="number" data-price="${p.id}" value="${p.base_price}"></td><td><input class="small-input" type="number" min=".01" step=".1" data-weight="${p.id}" value="${p.importance_weight}"></td></tr>`).join("")}</tbody></table>`;
 $("pool-table").querySelectorAll("[data-player]").forEach(el=>el.onchange=async()=>{await api(`/api/rooms/${roomCode}/players/${el.dataset.player}`,{method:"PATCH",body:JSON.stringify({included:el.checked})});const p=pool.find(x=>x.id==el.dataset.player);p.included=el.checked});
 $("pool-table").querySelectorAll("[data-price]").forEach(el=>el.onchange=async()=>{await api(`/api/rooms/${roomCode}/players/${el.dataset.price}`,{method:"PATCH",body:JSON.stringify({base_price:Number(el.value)})})});
 $("pool-table").querySelectorAll("[data-weight]").forEach(el=>el.onchange=async()=>{await api(`/api/rooms/${roomCode}/players/${el.dataset.weight}`,{method:"PATCH",body:JSON.stringify({importance_weight:Number(el.value)})})});
}
$("pool-search").oninput=renderPool;
async function updateHostFranchise(value){
 try{
  const d=await api(`/api/rooms/${roomCode}/host/select-franchise`,{method:"POST",body:JSON.stringify({franchise_id:value?Number(value):null})});
  const selected=(d.franchises||[]).find(f=>Number(f.id)===Number(value));
  myFranchise=selected?{id:selected.id,name:selected.name}:null;
  paintRoleLabel();
  render(d);
 }catch(e){alert(e.message);render(state)}
}
$("add-player").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await api(`/api/rooms/${roomCode}/players`,{method:"POST",body:JSON.stringify(Object.fromEntries(f))});e.target.reset();await loadPool()}catch(err){alert(err.message)}};
$("csv-upload").onchange=async e=>{const f=e.target.files[0];if(!f)return;const fd=new FormData();fd.append("file",f);try{const r=await fetch(`/api/rooms/${roomCode}/players/import-csv`,{method:"POST",headers:{"Authorization":"Bearer "+token},body:fd});const d=await r.json();if(!r.ok)throw Error(d.detail);alert("Imported "+d.imported+" players");await loadPool()}catch(err){alert(err.message)}};
function renderUnsold(list){
 const keep=new Set([...$("unsold-list").querySelectorAll('input[type="checkbox"]:checked')].map(x=>x.value));
 const prices={};$("unsold-list").querySelectorAll("[data-unsold-price]").forEach(x=>{if(x.value)prices[x.dataset.unsoldPrice]=x.value});
 $("unsold-list").innerHTML=list.map(p=>`<div class="unsold-row"><label class="u-pick"><input type="checkbox" value="${p.player_id}" ${keep.has(String(p.player_id))?"checked":""}><span class="u-name"><b>${esc(p.name)}</b><small>${esc(roleLabel(p.role))}${p.state_team?" · "+esc(p.state_team):""}</small></span></label><span class="u-base"><small>base</small><b>${money(p.base_price)}</b></span><input class="small-input u-price" type="number" min="1" inputmode="numeric" data-unsold-price="${p.player_id}" placeholder="New base" value="${esc(prices[p.player_id]||"")}" title="Optional new base price"></div>`).join("")||'<p class="muted">No unsold players yet.</p>'}
$("nominate").onclick=async()=>{
 const ids=[...$("unsold-list").querySelectorAll('input[type="checkbox"]:checked')].map(x=>Number(x.value));
 const price_overrides={};$("unsold-list").querySelectorAll("[data-unsold-price]").forEach(x=>{if(x.value.trim()!=="")price_overrides[x.dataset.unsoldPrice]=Number(x.value)});
 if(!ids.length){alert("Select at least one unsold player.");return;}
 const skipped=state?.queue_remaining||0;
 if(!confirm(`Start an unsold-only round with ${ids.length} selected players? This will skip ${skipped} unseen queued players and settle the current player immediately.`))return;
 try{const d=await api(`/api/rooms/${roomCode}/nominate-unsold`,{method:"POST",body:JSON.stringify({player_ids:ids,price_overrides})});render(d);}
 catch(e){alert(e.message)}
};
init();

// ---- credentials card, login form, password resets ---------------------------------------------
function inviteText(code,name,pw){return `Join the ${state?.room?.name||"SMAT"} auction\n${location.origin}\nRoom code: ${code}\nFranchise: ${name}\nPassword: ${pw}`}
async function copyText(t,btn){
 try{await navigator.clipboard.writeText(t)}catch(e){const a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy")}catch(_){}a.remove()}
 if(btn){const o=btn.textContent;btn.textContent="Copied";setTimeout(()=>btn.textContent=o,1400)}
}
let lastCreds=null;
function showCredentials(d){
 // Logins live only in the "Franchise passwords" panel at the bottom of the page. A room that was just created
 // keeps its creation-time passwords as a fallback in case the lookup fails.
 lastCreds=d;credData=null;
 const det=$("access-details");det.open=true;loadCredentials();
}
function scrollToPasswords(){const d=$("access-details");d.open=true;loadCredentials();$("access-panel").scrollIntoView({behavior:"smooth",block:"start"})}
const gp=$("goto-passwords");if(gp)gp.onclick=scrollToPasswords;
let namesTimer=null;
async function loadFranchiseNames(){
 const code=$("login-room").value.trim().toUpperCase(),sel=$("login-franchise");
 const reset=msg=>{sel.innerHTML=`<option value="">${msg}</option>`;sel.disabled=true};
 if(code.length<4){reset("Enter the room code first");return}
 try{
  const r=await fetch(`/api/rooms/${encodeURIComponent(code)}/franchise-names`);
  if(!r.ok){reset("Room not found");return}
  const d=await r.json(),keep=sel.value;
  // The host's own franchise is the host login: one entry, not a franchise plus a separate "Host".
  const opts=d.franchises.map(n=>n===d.host_franchise?`<option value="${esc(n)}">${esc(n)} (Host)</option>`:`<option value="${esc(n)}">${esc(n)}</option>`);
  if(!d.host_franchise)opts.push(`<option value="${esc(d.host_name)}">Host (organiser)</option>`);
  sel.innerHTML=`<option value="">Select your franchise</option>`+opts.join("");
  sel.disabled=false;if(keep&&[...sel.options].some(o=>o.value===keep))sel.value=keep;
 }catch(e){reset("Could not load. Check your connection")}
}
$("login-room").addEventListener("input",()=>{clearTimeout(namesTimer);namesTimer=setTimeout(loadFranchiseNames,300)});
$("login-form").addEventListener("submit",async e=>{
 e.preventDefault();const er=$("login-error"),btn=$("login-submit");er.classList.add("hidden");btn.disabled=true;
 try{
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
   room:$("login-room").value,franchise:$("login-franchise").value,password:$("login-password").value})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(typeof d.detail==="string"?d.detail:"Could not sign in. Please try again.");
  roomCode=d.room_code;token=d.token;saveSession();$("login-password").value="";await init();
 }catch(err){er.textContent=(err instanceof TypeError)?"No connection. Check your internet and try again.":err.message;er.classList.remove("hidden")}
 finally{btn.disabled=false}
});

$("host-more").onclick=()=>{
 const d=$("admin-controls"),open=d.classList.toggle("drawer-closed")===false;
 $("host-more").setAttribute("aria-expanded",open);
 try{localStorage.setItem("smat.hostDrawer",open?"1":"0")}catch(e){}
};
try{if(localStorage.getItem("smat.hostDrawer")==="1"){$("admin-controls").classList.remove("drawer-closed");$("host-more").setAttribute("aria-expanded","true")}}catch(e){}

// ---- "I'm out": step out of one player, visible to everyone ------------------------------------------
function paintOut(p){
 const strip=$("out-strip"),btn=$("out-button");if(!strip||!btn)return;
 const out=new Set(((p&&p.out_ids)||[]).map(Number)),list=state?.franchises||[];
 const mine=!!(myFranchise&&out.has(Number(myFranchise.id)));
 const outList=list.filter(f=>out.has(Number(f.id)));
 strip.classList.toggle("hidden",!outList.length);
 $("out-badges").innerHTML=outList.map(f=>`<span class="ob" title="${esc(f.name)} is out">${badgeHTML(f.name,f.id)}</span>`).join("");
 $("out-count").textContent=outList.length?`${outList.length} of ${list.length} out`:"";
 const leading=!!(p&&myFranchise&&Number(p.highest_franchise_id)===Number(myFranchise.id));
 const live=!!p&&state?.room?.status==="LIVE";
 btn.classList.toggle("is-out",mine);
 btn.textContent=mine?"Rejoin":"I'm out";
 btn.disabled=!myFranchise||!live||leading||outInFlight;
 btn.title=leading?"You are the highest bidder":mine?"Tell everyone you are bidding again":"Tell everyone you are not bidding on this player";
 document.querySelectorAll("#franchises details[data-franchise]").forEach(el=>el.classList.toggle("is-out",out.has(Number(el.dataset.franchise))));
 const mt=$("my-squad").querySelector(".team-card");if(mt)mt.classList.toggle("is-out",mine);
}
let outInFlight=false;
$("out-button").onclick=async()=>{
 const p=state?.current_player;if(!p||!myFranchise||outInFlight)return;
 const want=!new Set((p.out_ids||[]).map(Number)).has(Number(myFranchise.id));
 const before=[...(p.out_ids||[])];
 p.out_ids=want?[...before,Number(myFranchise.id)]:before.filter(i=>Number(i)!==Number(myFranchise.id));
 outInFlight=true;paintOut(p);
 try{await api(`/api/rooms/${roomCode}/pass`,{method:"POST",body:JSON.stringify({lot_id:p.lot_id,out:want})})}
 catch(e){p.out_ids=before;if(!(e instanceof AuthError))message(e.message)}
 finally{outInFlight=false;paintOut(state?.current_player)}
};

// ---- auction pool tab ------------------------------------------------------------------------------------
let poolData=null,poolFilter="UPCOMING",poolKey="",poolLoading=false;
function sideTabActive(){return document.querySelector(".side-tab.active")?.dataset.side}
async function loadAuctionPool(force){
 if(poolLoading||!roomCode||!token)return;
 poolLoading=true;
 try{poolData=await api(`/api/rooms/${roomCode}/auction-pool`);renderAuctionPool()}catch(e){}finally{poolLoading=false}
}
async function queuePlayer(body,btn){
 if(btn)btn.disabled=true;
 try{const d=await api(`/api/rooms/${roomCode}/queue-player`,{method:"POST",body:JSON.stringify(body)});render(d);loadAuctionPool();return true}
 catch(e){if(!(e instanceof AuthError))alert(e.message);return false}
 finally{if(btn)btn.disabled=false}
}
$("pool-view-list").addEventListener("click",e=>{const b=e.target.closest("[data-next]");if(b)queuePlayer({player_id:Number(b.dataset.next),position:"next"},b)});
$("live-add-player").onsubmit=async e=>{
 e.preventDefault();const f=Object.fromEntries(new FormData(e.target));
 const ok=await queuePlayer({new:{name:f.name,state_team:f.state_team,role:f.role,base_price:Number(f.base_price)||20},position:f.position},e.submitter);
 if(ok){e.target.reset();e.target.base_price.value=20}
};
function renderAuctionPool(){
 if(!poolData)return;
 for(const k of ["UPCOMING","SOLD","UNSOLD"])$("pc-"+k).textContent=(poolData.counts[k]||0)+(k==="UPCOMING"?(poolData.counts.LIVE||0):0);
 document.querySelectorAll(".pf").forEach(b=>b.classList.toggle("active",b.dataset.pool===poolFilter));
 const q=$("pool-view-search").value.trim().toLowerCase();
 let rows=poolData.players.filter(p=>poolFilter==="UPCOMING"?(p.status==="UPCOMING"||p.status==="LIVE"):p.status===poolFilter);
 if(q)rows=rows.filter(p=>`${p.name} ${p.state_team} ${roleLabel(p.role)} ${p.franchise||""}`.toLowerCase().includes(q));
 const order={BAT:0,AR:1,WK:2,BOWL:3};
 rows.sort((a,b)=>poolFilter==="SOLD"?b.price-a.price:(a.status==="LIVE"?-1:b.status==="LIVE"?1:0)||(order[a.role]??9)-(order[b.role]??9)||b.base_price-a.base_price||a.name.localeCompare(b.name));
 $("pool-view-list").innerHTML=rows.length?rows.map(p=>`<div class="pool-row${p.status==="LIVE"?" live":""}">
   <span class="role-dot" data-role="${esc(String(p.role||"").toUpperCase())}"></span>
   <div class="pr-name"><b>${esc(p.name)}</b><small>${esc(roleLabel(p.role))}${p.state_team?" · "+esc(p.state_team):""}</small></div>
   ${p.status==="SOLD"?`<div class="pr-end"><strong>${money(p.price)}</strong><small>${esc(p.franchise||"")}</small></div>`
    :p.status==="LIVE"?`<span class="chip lead on">On the block</span>`:`<div class="pr-end"><strong>${money(p.base_price)}</strong><small>base</small></div>${p.next?'<span class="chip nextup">Next up</span>':isHost&&state?.room?.status!=="COMPLETED"?`<button type="button" class="next-btn" data-next="${p.id}" title="Make ${esc(p.name)} the next player">Next</button>`:""}`}
  </div>`).join(""):`<p class="muted pool-empty">${q?"No players match your search.":poolFilter==="SOLD"?"Nobody has been sold yet.":poolFilter==="UNSOLD"?"No unsold players so far.":"No players left in the pool."}</p>`;
}
document.querySelectorAll(".pf").forEach(b=>b.onclick=()=>{poolFilter=b.dataset.pool;renderAuctionPool()});
$("pool-view-search").addEventListener("input",renderAuctionPool);
function refreshPoolIfNeeded(s){
 const key=`${s.room.status}|${s.current_player?.lot_id||""}|${s.queue_remaining||0}|${s.next_up?.player_id||""}|${(s.unsold||[]).length}|${s.franchises.reduce((t,f)=>t+f.squad_count,0)}`;
 if(key===poolKey)return;poolKey=key;
 if(sideTabActive()==="pool"||!poolData)loadAuctionPool();
}

// ---- passwords: bottom of the page, viewable any time ----------------------------------------------------
let credData=null;
async function loadCredentials(){
 try{credData=await api(`/api/rooms/${roomCode}/host/credentials`);renderAccess()}catch(e){}
}
function renderAccess(){
 const el=$("access-live");if(!el)return;
 if(!credData){el.innerHTML='<p class="muted">Loading…</p>';return}
 const hid=credData.host_franchise_id,hf=hid&&credData.franchises.find(f=>Number(f.id)===Number(hid));
 const made=lastCreds&&lastCreds.room_code===roomCode?lastCreds:null;
 const madePw=n=>made?(made.credentials||[]).find(c=>c.franchise===n)?.password:null;
 const rows=[{key:"host",label:hf?hf.name:credData.host.franchise,host:true,badgeId:hf?hf.id:0,password:credData.host.password||(made&&made.host_login?.password)||null},
  ...credData.franchises.filter(f=>!hf||f.id!==hf.id).map(f=>({key:String(f.id),id:f.id,label:f.name,badgeId:f.id,password:f.password||madePw(f.name)||null}))];
 el.innerHTML=`<p class="muted">Sign in with the room code, the franchise name and its password. Anyone can use their password on any number of devices.</p>
  <div class="room-code-line"><span class="lbl">Room code</span><b>${esc(roomCode)}</b><button class="ghost small" id="cred-copyall" type="button">Copy all invites</button></div>
  <div class="cred-list">${rows.map(r=>`<div class="cred-row${r.host?" host":""}" data-key="${r.key}">
   <div class="cred-name">${r.host?'<span class="host-tag">HOST</span>':badgeHTML(r.label,r.badgeId)}<b>${esc(r.label)}</b></div>
   <code class="cred-pw" data-shown="0">${r.password?"••••-••••":"Not viewable"}</code>
   <div class="cred-actions">${r.password?`<button class="ghost small" type="button" data-act="show">Show</button><button class="ghost small" type="button" data-act="copy">Copy invite</button>`:""}
   <button class="ghost small" type="button" data-act="reset">${r.password?"New password":"Issue password"}</button></div>
  </div>`).join("")}</div>`;
 const byKey=Object.fromEntries(rows.map(r=>[r.key,r]));
 el.querySelectorAll(".cred-row").forEach(row=>{
  const r=byKey[row.dataset.key],pw=row.querySelector(".cred-pw");
  row.querySelectorAll("[data-act]").forEach(b=>b.onclick=async()=>{
   const act=b.dataset.act;
   if(act==="show"){const on=pw.dataset.shown!=="1";pw.dataset.shown=on?"1":"0";pw.textContent=on?r.password:"••••-••••";b.textContent=on?"Hide":"Show"}
   else if(act==="copy")copyText(inviteText(roomCode,r.label,r.password),b);
   else if(act==="reset"){
    const who=r.host?"the host login":r.label;
    if(!confirm(`Issue a new password for ${who}? Anyone signed in with it will be signed out.`))return;
    try{
     const d=await api(`/api/rooms/${roomCode}/host/reset-password`,{method:"POST",body:JSON.stringify({franchise_id:r.host?null:r.id})});
     if(r.host){const rs=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({room:roomCode,franchise:"Host",password:d.password})});const j=await rs.json();token=j.token;saveSession();location.reload();return}
     await loadCredentials();
    }catch(err){alert(err.message)}
   }
  });
 });
 $("cred-copyall").onclick=e=>copyText(rows.filter(r=>r.password).map(r=>inviteText(roomCode,r.label,r.password)).join("\n\n"),e.target);
}
$("access-details").addEventListener("toggle",()=>{if($("access-details").open)loadCredentials()});
