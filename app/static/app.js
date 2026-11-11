const $ = (id) => document.getElementById(id);
let roomCode=null, token=null, isHost=false, myFranchise=null, ws=null, state=null, pool=[], timerInterval=null, lastSnapshot=null;
let hasInitialSnapshot=false, lastCelebrationEventId=null, audioContext=null;
let reconnectDelay=1000, reconnectTimer=null, heartbeatTimer=null;
// Real-time sync state. `state` is the single source of truth in the browser; full snapshots replace
// it, compact bid events patch it. Every message is ordered by the server's room version.
let bidInFlight=false, armedBid=null, resyncing=false, bidFieldsVersion=0;
window.__bidStats=[];
// ---- session: team + password login, kept on this device -------------------------------------
// The browser keeps the credential (sessionStorage per tab, localStorage as a fallback) so reloads,
// lost connections and closed tabs never sign anyone out. Only an explicit "Log out" or a password
// the host has reset clears it.
const SKEY="smat_session";
const store=(w)=>{try{return window[w]}catch(e){return null}};
function saveSession(s){for(const w of ["sessionStorage","localStorage"]){try{store(w)?.setItem(SKEY,JSON.stringify(s))}catch(e){}}}
function loadSession(){for(const w of ["sessionStorage","localStorage"]){try{const v=store(w)?.getItem(SKEY);if(v){const o=JSON.parse(v);if(o&&o.room&&o.token)return o}}catch(e){}}return null}
function clearSession(){for(const w of ["sessionStorage","localStorage"]){try{store(w)?.removeItem(SKEY)}catch(e){}}}
const params=new URLSearchParams(location.search);
roomCode=params.get("room"); token=params.get("token");
if(roomCode&&token){saveSession({room:roomCode.toUpperCase(),token});try{history.replaceState({},"",location.pathname)}catch(e){}}   // direct-login links still work
else{const sess=loadSession();if(sess){roomCode=sess.room;token=sess.token}}
const money=(n)=>Number(n||0).toLocaleString("en-IN");
function auth(){return {"Authorization":"Bearer "+token,"Content-Type":"application/json"}}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...auth(),...(opts.headers||{})}});let d=await r.json().catch(()=>({}));if(!r.ok){const err=new Error(typeof d.detail==="string"?d.detail:"Request failed");err.status=r.status;throw err}return d}
function show(id){$(id).classList.remove("hidden")} function hide(id){$(id).classList.add("hidden")}
function message(text){$("bid-feedback").textContent=text}
function roleText(){return isHost?(myFranchise?"Host · "+myFranchise.name:"Host (Administrator)"):(myFranchise?myFranchise.name:"Guest (view only)")}
function showLogin(msg){
 hide("room");hide("landing");show("login");
 const last=(()=>{try{return localStorage.getItem("smat_last_room")||""}catch(e){return ""}})();
 if(last&&!$("login-room").value){$("login-room").value=last;loadTeams()}
 const err=$("login-error");if(msg){err.textContent=msg;err.classList.remove("hidden")}else err.classList.add("hidden");
}
let teamsFor="";
async function loadTeams(){
 const code=$("login-room").value.trim().toUpperCase(),sel=$("login-team"),err=$("login-error");
 if(code.length<4){sel.disabled=true;sel.innerHTML='<option value="">Enter the room code first</option>';teamsFor="";return}
 if(code===teamsFor)return;
 try{
  const r=await fetch(`/api/rooms/${encodeURIComponent(code)}/teams`);const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.detail||"No auction found with that room code");
  teamsFor=code;err.classList.add("hidden");
  sel.innerHTML='<option value="">Choose your team…</option>'+d.teams.map(t=>`<option value="${t.id}">${esc(t.name)}</option>`).join("")+'<option value="host">Host (administrator)</option>';
  sel.disabled=false;
 }catch(e){teamsFor="";sel.disabled=true;sel.innerHTML='<option value="">—</option>';err.textContent=e.message;err.classList.remove("hidden")}
}
async function init(){
 if(!roomCode||!token){showLogin();return}
 hide("login");hide("landing");show("room");
 try{
   const me=await api(`/api/rooms/${roomCode}/me`);
   const snap=await api(`/api/rooms/${roomCode}/snapshot`);
   isHost=me.is_host;
   myFranchise=me.franchise;
   $("role-label").textContent=roleText();
   try{localStorage.setItem("smat_last_room",roomCode)}catch(e){}
   if(isHost){
    if(snap.room.status==="SETUP"){show("host-setup");await loadPool();showStoredCredentials(snap)}
    else {show("live-area");show("admin-controls")}
   }
   else if(myFranchise){show("live-area")}
   else {show("live-area");$("bid-button").disabled=true;message("You are viewing only. Log in with a team password to bid.")}
   await render(snap); connect();
 }catch(e){
  if(e.status===401||e.status===403||e.status===404){
   // The credential is genuinely invalid (wrong, reset by the host, or the room is gone).
   clearSession();roomCode=token=null;
   showLogin(e.status===404?"That auction no longer exists.":"Your login is no longer valid. Please sign in again.");
  }else{
   // Network trouble is NOT a reason to sign anyone out: keep the session and keep trying.
   $("connection").textContent="Reconnecting…";$("connection").classList.remove("ok");
   setTimeout(init,2000);
  }
 }
}
async function render(s){
 // Concurrent bid responses can arrive out of order. Never let an older
 // committed room version overwrite a newer snapshot already rendered.
 if(state && Number(state.room?.version||0)>Number(s.room?.version||0))return;
 state=s;lastSnapshot=s;bidFieldsVersion=0;
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
  if(newResults.length)celebrate(newResults[newResults.length-1]);
  lastCelebrationEventId=Math.max(lastCelebrationEventId||0,newestEventId);
 }
 $("connection").textContent=ws&&ws.readyState===1?"Connected":"Connecting…";$("connection").classList.toggle("ok",!!(ws&&ws.readyState===1));
 const expandedFranchises=new Set([...document.querySelectorAll("#franchises details[open]")].map(el=>String(el.dataset.franchise)));
 $("franchises").innerHTML=s.franchises.map(f=>{
  const own=myFranchise&&Number(myFranchise.id)===Number(f.id);
  const squad=f.squad||[];
  const expanded=own||expandedFranchises.has(String(f.id));
  return `<details data-franchise="${f.id}" class="franchise ${s.current_player&&s.current_player.highest_franchise_id===f.id?"leading-team":""}" ${expanded?"open":""}>
   <summary><span class="fr-avatar" style="--hue:${hueFor(f.name)}">${esc(initials(f.name))}</span><div class="fr-text"><strong>${esc(f.name)}${own?" · MY TEAM":""}</strong><div class="meta">Purse: ${money(f.purse_remaining)} · Squad: ${f.squad_count}/${s.room.max_squad} <span class="expand-hint">Click to view squad</span></div></div></summary>
   <div class="franchise-squad">${squad.length?`<table><thead><tr><th>Player</th><th>Role</th><th>Price</th></tr></thead><tbody>${squad.map(x=>`<tr><td>${esc(x.player)}</td><td>${esc(x.role||"—")}</td><td>${money(x.price)}</td></tr>`).join("")}</tbody></table>`:`<span class="muted">No players purchased yet.</span>`}</div>
  </details>`;
 }).join("");
 $("activity").innerHTML=s.events.map(e=>`<div class="activity-item kind-${esc(e.kind||"")}">${esc(e.message)}</div>`).join("")||'<p class="muted">No activity yet.</p>';
 if(s.room.status === "COMPLETED"){
  hide("live-area");hide("host-setup");hide("admin-controls");show("final-results");
  renderFinalResults(s);
  clearInterval(timerInterval);
  return;
 }
 hide("final-results");
 if(s.current_player){
  const p=s.current_player;show("live-area");
  clearTimeout(revealTimer);
  const lead=leadInSeconds(s,p);
  if(lead>0.15){
   // The previous player's result banner is still showing: keep the new player hidden (and unbiddable)
   // until the server's lead-in ends. The overlay is cut short to end exactly then.
   setPending(true);
   const root=$("celebration");clearTimeout(root._hideTimer);root._hideTimer=setTimeout(()=>root.classList.add("hidden"),lead*1000);
   $("timer").textContent="—";$("timer").classList.remove("urgent");clearInterval(timerInterval);
   revealTimer=setTimeout(()=>{if(state===s||state?.current_player?.lot_id===p.lot_id){setPending(false);paintBid();startLocalTimer(state.current_player.ends_at)}},lead*1000+30);
  }else setPending(false);
  $("player-name").textContent=p.name;$("player-role").textContent=p.role;$("player-team").textContent=p.state_team;
  $("round-label").textContent="ROUND "+p.round_no;$("base-price").textContent=money(p.base_price);$("current-bid").textContent=money(p.current_bid||p.base_price);
  paintBid();
  if(lead<=0.15)startLocalTimer(p.ends_at);
 }else{
  setPending(false);
  $("player-name").textContent=s.room.status==="COMPLETED"?"Auction completed":"Waiting for next player";
  $("player-team").textContent="Players remaining: "+s.queue_remaining;$("timer").textContent="—";
  updateBidButton();
 }
 // Squad information must refresh even between players, immediately after a sale.
 $("my-squad").innerHTML=myFranchise?((s.franchises.find(f=>f.id===myFranchise.id)?.squad||[]).map(x=>`${esc(x.player)} — ${money(x.price)}`).join("<br>")||"No players purchased yet"):"Administrator view — choose a franchise under More host options to bid and see its squad.";
 if(isHost){
  show("admin-controls");renderUnsold(s.unsold);
  const status=s.room.status;
  paintHostButtons(s);
  const warning=$("unsold-warning");
  if(warning)warning.textContent=`${s.queue_remaining||0} unseen players remain in the queue. Starting this unsold-only round will skip them and auction only the players you select. The current player, if any, will be settled first.`;
  const nominate=$("nominate");
  if(nominate)nominate.disabled=!(s.unsold||[]).length || status==="COMPLETED";
  const livePicker=$("host-franchise-live");
  if(livePicker){livePicker.innerHTML=hostFranchiseOptions(s);livePicker.value=s.room.host_franchise_id||"";}
 }
}
function paintHostButtons(s){
 const status=s.room.status, history=s.history||{};
 const controls={pause:status!=="LIVE",resume:status!=="PAUSED",
  "undo-bid":!history.can_undo_bid,"redo-bid":!history.can_redo_bid,
  "undo-sale":!history.can_undo_sale,"redo-sale":!history.can_redo_sale,
  finish:status==="COMPLETED"};
 document.querySelectorAll("[data-action]").forEach(b=>b.disabled=!!controls[b.dataset.action]);
}
function hostFranchiseOptions(s){
 return `<option value="">Host only — not bidding</option>`+(s.franchises||[]).map(f=>`<option value="${f.id}">${esc(f.name)}</option>`).join("");
}
function syncHostFranchisePickers(s){
 const options=hostFranchiseOptions(s);
 ["host-franchise-setup","host-franchise-live"].forEach(id=>{
  const el=$(id);if(!el)return;el.innerHTML=options;el.value=s.room.host_franchise_id||"";
 });
}
function renderFinalResults(s){
 const franchises=s.franchises||[];
 const totalPlayers=franchises.reduce((n,f)=>n+(f.squad_count||0),0);
 const totalSpent=franchises.reduce((n,f)=>n+Math.max(0,(s.room.purse||0)-(f.purse_remaining||0)),0);
 $("final-summary").innerHTML=`
  <div class="final-stat panel"><span>FRANCHISES</span><strong>${franchises.length}</strong></div>
  <div class="final-stat panel"><span>PLAYERS SOLD</span><strong>${totalPlayers}</strong></div>
  <div class="final-stat panel"><span>TOTAL SPENT</span><strong>${money(totalSpent)}</strong></div>`;
 $("final-franchises").innerHTML=franchises.map(f=>{
  const squad=f.squad||[];
  const spent=Math.max(0,(s.room.purse||0)-(f.purse_remaining||0));
  return `<article class="final-team panel">
   <div class="final-team-head"><div><div class="eyebrow">FINAL SQUAD</div><h3>${esc(f.name)}</h3></div><div class="final-team-count">${squad.length} / ${s.room.max_squad}</div></div>
   <div class="final-team-metrics"><div><span>Spent</span><strong>${money(spent)}</strong></div><div><span>Remaining purse</span><strong>${money(f.purse_remaining)}</strong></div></div>
   ${squad.length?`<div class="table-wrap"><table class="final-squad-table"><thead><tr><th>#</th><th>Player</th><th>State team</th><th>Role</th><th>Price</th></tr></thead><tbody>${squad.map((p,i)=>`<tr><td>${i+1}</td><td>${esc(p.player)}</td><td>${esc(p.state_team||"—")}</td><td>${esc(p.role||"—")}</td><td>${money(p.price)}</td></tr>`).join("")}</tbody></table></div>`:`<p class="muted">No players purchased.</p>`}
  </article>`;
 }).join("");
}

function celebrate(event){
  const sold=event.kind==="SOLD";
  const data=event.data||{};
  const player=(event.message||"").split(sold?" sold to ":" went unsold")[0]||"Player";
  const price=data.price;
  $("celebration-title").textContent=sold?"PLAYER SOLD!":"PLAYER UNSOLD";
  $("celebration-detail").textContent=sold?`${player} sold for ${money(price)}${data.franchise_name?" to "+data.franchise_name:""}`:`${player} went unsold`;
  const root=$("celebration"), layer=$("confetti-layer");
  layer.innerHTML="";
  // Both outcomes get a visual particle effect: bright celebratory confetti
  // for sales, and slower muted paper for unsold players.
  const particleCount=sold?90:34;
  for(let i=0;i<particleCount;i++){
    const piece=document.createElement("span");
    piece.className=sold?"confetti-piece":"confetti-piece unsold-confetti-piece";
    piece.style.left=(Math.random()*100)+"%";
    piece.style.animationDelay=(Math.random()*(sold?0.8:0.35))+"s";
    piece.style.animationDuration=(sold?2.2+Math.random()*1.8:2.8+Math.random()*1.8)+"s";
    piece.style.setProperty("--drift",(Math.random()*(sold?240:120)-(sold?120:60))+"px");
    layer.appendChild(piece);
  }
  root.classList.remove("hidden");
  root.classList.toggle("unsold-celebration",!sold);
  playCelebrationSound(sold);
  clearTimeout(root._hideTimer);
  root._hideTimer=setTimeout(()=>root.classList.add("hidden"),sold?4800:3600);
}
function prepareCelebrationAudio(){
  try{
    const AudioCtx=window.AudioContext||window.webkitAudioContext;
    if(!AudioCtx)return;
    audioContext=audioContext||new AudioCtx();
    if(audioContext.state==="suspended")audioContext.resume();
  }catch(e){/* Sound may be unavailable in this browser. */}
}
document.addEventListener("pointerdown",prepareCelebrationAudio,{once:true});
function playCelebrationSound(sold){
  try{
    const AudioCtx=window.AudioContext||window.webkitAudioContext;
    if(!AudioCtx)return;
    audioContext=audioContext||new AudioCtx();
    if(audioContext.state==="suspended")audioContext.resume();
    const now=audioContext.currentTime;
    const notes=sold?[523.25,659.25,783.99,1046.5]:[392,329.63,261.63];
    notes.forEach((freq,i)=>{
      const osc=audioContext.createOscillator(), gain=audioContext.createGain();
      osc.type=sold?"triangle":"sine";osc.frequency.value=freq;
      gain.gain.setValueAtTime(0.0001,now+i*0.14);
      gain.gain.exponentialRampToValueAtTime(sold?0.13:0.10,now+i*0.14+0.025);
      gain.gain.exponentialRampToValueAtTime(0.0001,now+i*0.14+0.22);
      osc.connect(gain);gain.connect(audioContext.destination);
      osc.start(now+i*0.14);osc.stop(now+i*0.14+0.24);
    });
  }catch(e){/* Audio is optional; visual celebration still works. */}
}
let revealTimer=null, pendingReveal=false;
function leadInSeconds(s,p){
 if(!p||!p.ends_at||s.room.status!=="LIVE")return 0;
 const iso=/([zZ]|[+-]\d{2}:?\d{2})$/.test(p.ends_at)?p.ends_at:p.ends_at+"Z";
 return (new Date(iso).getTime()-Date.now())/1000-(Number(s.room.timer_seconds)||10);
}
function setPending(on){
 pendingReveal=on;
 $("intermission").classList.toggle("hidden",!on);$("player-main").classList.toggle("hidden",on);
 document.querySelector(".player-panel").classList.toggle("pending",on);
 updateBidButton();
}
function initials(name){const w=String(name||"").trim().split(/\s+/).filter(Boolean);return (w.length>1?w[0][0]+w[1][0]:String(name||"?").slice(0,2)).toUpperCase()}
function hueFor(name){let h=0;for(const c of String(name||""))h=(h*31+c.charCodeAt(0))%360;return h}
function paintTimer(sec){
 const t=$("timer"),fill=$("timer-fill"),total=Number(state?.room?.timer_seconds)||10;
 t.textContent=sec.toFixed(1)+"s";t.classList.toggle("urgent",sec>0&&sec<=3);
 if(fill)fill.style.width=Math.max(0,Math.min(100,sec/total*100))+"%";
}
function startLocalTimer(endsAt){
 clearInterval(timerInterval);
 if(!endsAt){$("timer").textContent="PAUSED";$("timer").classList.remove("urgent");return}
 const normalizedEndsAt=/([zZ]|[+-]\d{2}:?\d{2})$/.test(endsAt)?endsAt:endsAt+"Z";
 const deadline=new Date(normalizedEndsAt).getTime();
 const update=()=>paintTimer(Math.max(0,(deadline-Date.now())/1000));
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
 paintBid();startLocalTimer(p.ends_at);
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
 $("leading").textContent=p.highest_franchise?"Leading: "+p.highest_franchise:"No bids yet";
 $("next-bid").textContent=money(p.next_bid);
 // Highlight the leader without rebuilding the franchise cards (keeps expanded squads open).
 document.querySelectorAll("#franchises details[data-franchise]").forEach(el=>
  el.classList.toggle("leading-team",Number(el.dataset.franchise)===Number(p.highest_franchise_id)));
 updateBidButton();
}
function updateBidButton(){
 const b=$("bid-button"),p=state?.current_player;
 if(isHost&&!myFranchise){hide("bid-button");return}
 show("bid-button");
 const leading=!!(p&&myFranchise&&Number(p.highest_franchise_id)===Number(myFranchise.id));
 b.disabled=!myFranchise||!p||state?.room?.status!=="LIVE"||bidInFlight||leading||pendingReveal;
}
function prependActivity(e){
 const box=$("activity");if(!box)return;
 const placeholder=box.querySelector("p.muted");if(placeholder)placeholder.remove();
 const div=document.createElement("div");div.className="activity-item kind-"+String(e.kind||"").replace(/[^A-Z_]/g,"");div.textContent=e.message;
 box.prepend(div);
 while(box.children.length>30)box.lastElementChild.remove();
}
function connect(){
 if(!roomCode||!token)return;
 if(ws&&(ws.readyState===WebSocket.OPEN||ws.readyState===WebSocket.CONNECTING))return;
 const proto=location.protocol==="https:"?"wss":"ws";
 const socket=new WebSocket(`${proto}://${location.host}/ws/${roomCode}?token=${encodeURIComponent(token)}`);
 ws=socket;
 socket.onopen=()=>{
  reconnectDelay=1000;$("connection").textContent="Connected";$("connection").classList.add("ok");
  clearInterval(heartbeatTimer);
  heartbeatTimer=setInterval(()=>{if(socket.readyState===WebSocket.OPEN)socket.send("ping")},25000);
 };
 socket.onclose=()=>{
  clearInterval(heartbeatTimer);
  if(ws===socket)ws=null;
  $("connection").textContent="Reconnecting…";$("connection").classList.remove("ok");
  if(!reconnectTimer){
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
$("create-form").addEventListener("submit",async e=>{
 e.preventDefault();const fd=new FormData(e.target);
 const franchises=fd.get("franchises").split("\n").map(x=>x.trim()).filter(Boolean);
 try{
  const d=await fetch("/api/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
   name:fd.get("name"),franchises,purse:Number(fd.get("purse")),timer_seconds:Number(fd.get("timer_seconds")),
   min_squad:Number(fd.get("min_squad")),max_squad:Number(fd.get("max_squad")),bid_extension:fd.get("bid_extension")==="on"
  })}).then(async r=>{const x=await r.json();if(!r.ok)throw Error(x.detail);return x});
  saveCreds(d);
  $("invite-output").innerHTML=credentialsHtml(d.room_code,{host:d.host_password,teams:Object.fromEntries(d.franchise_invites.map(x=>[x.franchise_id,x.password]))},d.franchise_invites.map(x=>({id:x.franchise_id,name:x.franchise})),true);
  wireCredentials($("invite-output"));show("invite-output");hide("landing");
  roomCode=d.room_code;token=d.host_token;isHost=true;saveSession({room:roomCode,token});await init();
 }catch(err){alert(err.message)}
});
// ---- team logins (shown to the host) ----------------------------------------------------------
function saveCreds(d){try{localStorage.setItem("smat_creds_"+d.room_code,JSON.stringify({host:d.host_password,teams:Object.fromEntries(d.franchise_invites.map(x=>[x.franchise_id,x.password]))}))}catch(e){}}
function loadCreds(code){try{return JSON.parse(localStorage.getItem("smat_creds_"+code))||{teams:{}}}catch(e){return {teams:{}}}}
function loginMessage(code,team,pw){return `SMAT Auction\nRoom code: ${code}\nTeam: ${team}\nPassword: ${pw}\nOpen ${location.origin} and tap "Join auction".`}
function credentialsHtml(code,c,teams,fresh){
 const rows=teams.map(t=>{const pw=(c.teams||{})[t.id];return `<tr><td>${esc(t.name)}</td><td class="pw">${pw?esc(pw):'<span class="muted">not stored on this device</span>'}</td><td class="cred-actions">${pw?`<button type="button" class="secondary" data-copy="${esc(loginMessage(code,t.name,pw))}">Copy message</button>`:""}<button type="button" class="secondary" data-reset="${t.id}">New password</button></td></tr>`}).join("");
 const host=c.host?`<tr><td>Host</td><td class="pw">${esc(c.host)}</td><td class="cred-actions"><button type="button" class="secondary" data-copy="${esc(loginMessage(code,"Host (administrator)",c.host))}">Copy message</button></td></tr>`:"";
 return `<div class="section-head"><h2>${fresh?"Auction created":"Team logins"}</h2><span class="muted">Room code <b class="roomcode">${esc(code)}</b></span></div>
  <p class="muted">${fresh?"Send each franchise its message below. ":""}Passwords are stored only on this device; the server keeps just a scrambled copy. “New password” replaces a lost one.</p>
  <div class="table-wrap"><table><thead><tr><th>Team</th><th>Password</th><th></th></tr></thead><tbody>${rows}${host}</tbody></table></div>
  <div class="button-row" style="margin-top:12px"><button type="button" class="primary" data-copy-all="1">Copy all messages</button></div>`;
}
function wireCredentials(box){
 const code=roomCode;
 box.onclick=async(ev)=>{
  const t=ev.target.closest("button");if(!t)return;
  if(t.dataset.copy!==undefined){await copyText(t.dataset.copy);t.textContent="Copied ✓";setTimeout(()=>t.textContent="Copy message",1500)}
  else if(t.dataset.copyAll){const c=loadCreds(code),names=(state?.franchises||[]);
   await copyText(names.filter(f=>c.teams[f.id]).map(f=>loginMessage(code,f.name,c.teams[f.id])).join("\n\n"));t.textContent="Copied ✓";setTimeout(()=>t.textContent="Copy all messages",1500)}
  else if(t.dataset.reset){
   if(!confirm("Create a new password for this team? The old one stops working."))return;
   try{const d=await api(`/api/rooms/${code}/host/reset-password`,{method:"POST",body:JSON.stringify({franchise_id:Number(t.dataset.reset)})});
    const c=loadCreds(code);c.teams=c.teams||{};c.teams[d.franchise_id]=d.password;try{localStorage.setItem("smat_creds_"+code,JSON.stringify(c))}catch(e){}
    renderCredsBox()}catch(e){alert(e.message)}
  }
 };
}
async function copyText(s){try{await navigator.clipboard.writeText(s)}catch(e){const ta=document.createElement("textarea");ta.value=s;document.body.appendChild(ta);ta.select();try{document.execCommand("copy")}catch(_){}ta.remove()}}
function showStoredCredentials(snap){
 // Setup phase: put the team logins at the top so the host can send them out before starting.
 const c=loadCreds(roomCode);if(!c.host&&!Object.keys(c.teams||{}).length)return;
 $("invite-output").innerHTML=credentialsHtml(roomCode,c,(snap.franchises||[]).map(f=>({id:f.id,name:f.name})),false);
 wireCredentials($("invite-output"));show("invite-output");
}
function renderCredsBox(){
 const box=$("creds-box");if(!box||!state)return;
 box.innerHTML=credentialsHtml(roomCode,loadCreds(roomCode),(state.franchises||[]).map(f=>({id:f.id,name:f.name})),false);wireCredentials(box);
}
$("show-creds").onclick=()=>{const box=$("creds-box");if(box.classList.contains("hidden")){renderCredsBox();box.classList.remove("hidden")}else box.classList.add("hidden")};
// ---- login form / log out -------------------------------------------------------------------------
$("login-room").addEventListener("input",()=>{clearTimeout($("login-room")._t);$("login-room")._t=setTimeout(loadTeams,250)});
$("login-room").addEventListener("blur",loadTeams);
$("login-show").onchange=e=>{$("login-password").type=e.target.checked?"text":"password"};
$("show-create").onclick=()=>{hide("login");show("landing")};
$("show-login").onclick=()=>{hide("landing");showLogin()};
$("login-form").addEventListener("submit",async e=>{
 e.preventDefault();const err=$("login-error"),btn=$("login-submit");
 const code=$("login-room").value.trim().toUpperCase(),team=$("login-team").value,pw=$("login-password").value;
 if(!team){err.textContent="Choose your team.";err.classList.remove("hidden");return}
 btn.disabled=true;btn.textContent="Signing in…";err.classList.add("hidden");
 try{
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({room_code:code,team,password:pw})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(typeof d.detail==="string"?d.detail:"Could not sign in");
  saveSession({room:d.room_code,token:d.token});
  roomCode=d.room_code;token=d.token;
  location.reload();            // start from a clean page state for the new identity
 }catch(ex){err.textContent=ex instanceof TypeError?"Could not reach the server. Check your connection and try again.":ex.message;err.classList.remove("hidden");btn.disabled=false;btn.textContent="Join auction"}
});
$("logout").onclick=()=>{if(!confirm("Log out? You will need your team password to get back in."))return;clearSession();location.reload()};
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
  case "PLAYER_NOT_STARTED":return "The next player is about to start — bidding opens in a moment.";
  default:return d.detail||"Bid rejected";
 }
}
$("start-auction").onclick=async()=>{if(!confirm("Lock the player pool and start the auction?"))return;try{const d=await api(`/api/rooms/${roomCode}/start`,{method:"POST"});render(d);show("live-area");hide("host-setup");hide("invite-output");}catch(e){alert(e.message)}};
document.querySelectorAll("[data-action]").forEach(b=>b.onclick=async()=>{const action=b.dataset.action;if(action==="finish"&&!confirm("End the auction?"))return;try{const d=await api(`/api/rooms/${roomCode}/host/${action}`,{method:"POST"});render(d)}catch(e){alert(e.message)}});
async function loadPool(){pool=await api(`/api/rooms/${roomCode}/pool`);renderPool()}
function renderPool(){
 const q=$("pool-search").value.toLowerCase();
 const rows=pool.filter(p=>(p.name+" "+p.state_team+" "+p.role).toLowerCase().includes(q));
 $("pool-table").innerHTML=`<table><thead><tr><th>Include</th><th>Player</th><th>State team</th><th>Role</th><th>Base</th><th>Importance</th></tr></thead><tbody>${rows.map(p=>`<tr><td><input type="checkbox" data-player="${p.id}" ${p.included?"checked":""}></td><td>${esc(p.name)}</td><td>${esc(p.state_team)}</td><td>${esc(p.role)}</td><td><input class="small-input" type="number" data-price="${p.id}" value="${p.base_price}"></td><td><input class="small-input" type="number" min=".01" step=".1" data-weight="${p.id}" value="${p.importance_weight}"></td></tr>`).join("")}</tbody></table>`;
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
  $("role-label").textContent=roleText();
  render(d);
 }catch(e){alert(e.message);render(state)}
}
$("host-franchise-setup").onchange=e=>updateHostFranchise(e.target.value);
$("add-player").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await api(`/api/rooms/${roomCode}/players`,{method:"POST",body:JSON.stringify(Object.fromEntries(f))});e.target.reset();await loadPool()}catch(err){alert(err.message)}};
$("csv-upload").onchange=async e=>{const f=e.target.files[0];if(!f)return;const fd=new FormData();fd.append("file",f);try{const r=await fetch(`/api/rooms/${roomCode}/players/import-csv`,{method:"POST",headers:{"Authorization":"Bearer "+token},body:fd});const d=await r.json();if(!r.ok)throw Error(d.detail);alert("Imported "+d.imported+" players");await loadPool()}catch(err){alert(err.message)}};
function renderUnsold(list){$("unsold-list").innerHTML=list.map(p=>`<div class="unsold-row"><label><input type="checkbox" value="${p.player_id}"> ${esc(p.name)} (${esc(p.role)}) — base ${money(p.base_price)}</label><input class="small-input" type="number" min="1" data-unsold-price="${p.player_id}" placeholder="Keep ${p.base_price}" title="Optional new base price"></div>`).join("")||'<p class="muted">No unsold players yet.</p>'}
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
