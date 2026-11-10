const $ = (id) => document.getElementById(id);
let roomCode=null, token=null, isHost=false, myFranchise=null, ws=null, state=null, pool=[], timerInterval=null, lastSnapshot=null;
let hasInitialSnapshot=false, lastCelebrationEventId=null, audioContext=null;
let reconnectDelay=1000, reconnectTimer=null, heartbeatTimer=null;
// Real-time sync state. `state` is the single source of truth in the browser; full snapshots replace
// it, compact bid events patch it. Every message is ordered by the server's room version.
let bidInFlight=false, armedBid=null, resyncing=false, bidFieldsVersion=0;
window.__bidStats=[];
const params=new URLSearchParams(location.search);
roomCode=params.get("room"); token=params.get("token");
const money=(n)=>Number(n||0).toLocaleString("en-IN");
function auth(){return {"Authorization":"Bearer "+token,"Content-Type":"application/json"}}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...auth(),...(opts.headers||{})}});let d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof d.detail==="string"?d.detail:"Request failed");return d}
function show(id){$(id).classList.remove("hidden")} function hide(id){$(id).classList.add("hidden")}
function message(text){$("bid-feedback").textContent=text}
async function init(){
 if(!roomCode||!token){show("landing");hide("room");return}
 hide("landing");show("room");$("room-code").textContent=roomCode;
 try{
   const me=await api(`/api/rooms/${roomCode}/me`);
   const snap=await api(`/api/rooms/${roomCode}/snapshot`);
   isHost=me.is_host;
   myFranchise=me.franchise;
   $("role-label").textContent=isHost?(myFranchise?" · Host / "+myFranchise.name:" · Administrator"):(myFranchise?" · "+myFranchise.name:" · Guest");
   if(isHost){
    if(snap.room.status==="SETUP"){show("host-setup");await loadPool();show("invite-list")}
    else {show("live-area");show("admin-controls")}
   }
   else if(myFranchise){show("live-area")}
   else {show("live-area");$("bid-button").disabled=true;message("Open your franchise-specific invite link to enable bidding.")}
   await render(snap); connect();
 }catch(e){alert(e.message);location.href="/"}
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
 $("connection").textContent=ws&&ws.readyState===1?"Connected":"Connecting…";
 const expandedFranchises=new Set([...document.querySelectorAll("#franchises details[open]")].map(el=>String(el.dataset.franchise)));
 $("franchises").innerHTML=s.franchises.map(f=>{
  const own=myFranchise&&Number(myFranchise.id)===Number(f.id);
  const squad=f.squad||[];
  const expanded=own||expandedFranchises.has(String(f.id));
  return `<details data-franchise="${f.id}" class="franchise ${s.current_player&&s.current_player.highest_franchise_id===f.id?"leading-team":""}" ${expanded?"open":""}>
   <summary><strong>${esc(f.name)}${own?" · MY TEAM":""}</strong><div class="meta">Purse: ${money(f.purse_remaining)} · Squad: ${f.squad_count}/${s.room.max_squad} <span class="expand-hint">Click to view squad</span></div></summary>
   <div class="franchise-squad">${squad.length?`<table><thead><tr><th>Player</th><th>Role</th><th>Price</th></tr></thead><tbody>${squad.map(x=>`<tr><td>${esc(x.player)}</td><td>${esc(x.role||"—")}</td><td>${money(x.price)}</td></tr>`).join("")}</tbody></table>`:`<span class="muted">No players purchased yet.</span>`}</div>
  </details>`;
 }).join("");
 $("activity").innerHTML=s.events.map(e=>`<div class="activity-item">${esc(e.message)}</div>`).join("")||'<p class="muted">No activity yet.</p>';
 if(s.room.status === "COMPLETED"){
  hide("live-area");hide("host-setup");hide("admin-controls");show("final-results");
  renderFinalResults(s);
  clearInterval(timerInterval);
  return;
 }
 hide("final-results");
 if(s.current_player){
  const p=s.current_player;show("live-area");
  $("player-name").textContent=p.name;$("player-role").textContent=p.role;$("player-team").textContent=p.state_team;
  $("round-label").textContent="ROUND "+p.round_no;$("base-price").textContent=money(p.base_price);$("current-bid").textContent=money(p.current_bid||p.base_price);
  paintBid();
  startLocalTimer(p.ends_at);
 }else{
  $("player-name").textContent=s.room.status==="COMPLETED"?"Auction completed":"Waiting for next player";
  $("player-team").textContent="Players remaining: "+s.queue_remaining;$("timer").textContent="—";
  updateBidButton();
 }
 // Squad information must refresh even between players, immediately after a sale.
 $("my-squad").innerHTML=myFranchise?((s.franchises.find(f=>f.id===myFranchise.id)?.squad||[]).map(x=>`${esc(x.player)} — ${money(x.price)}`).join("<br>")||"No players purchased yet"):"Administrator view — select a franchise invite to view its squad.";
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
function startLocalTimer(endsAt){
 clearInterval(timerInterval);
 if(!endsAt){$("timer").textContent="PAUSED";return}
 const normalizedEndsAt=/([zZ]|[+-]\d{2}:?\d{2})$/.test(endsAt)?endsAt:endsAt+"Z";
 const deadline=new Date(normalizedEndsAt).getTime();
 const update=()=>{const sec=Math.max(0,(deadline-Date.now())/1000);$("timer").textContent=sec.toFixed(1)+"s";};
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
 b.disabled=!myFranchise||!p||state?.room?.status!=="LIVE"||bidInFlight||leading;
}
function prependActivity(e){
 const box=$("activity");if(!box)return;
 const placeholder=box.querySelector("p.muted");if(placeholder)placeholder.remove();
 const div=document.createElement("div");div.className="activity-item";div.textContent=e.message;
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
  reconnectDelay=1000;$("connection").textContent="Connected";
  clearInterval(heartbeatTimer);
  heartbeatTimer=setInterval(()=>{if(socket.readyState===WebSocket.OPEN)socket.send("ping")},25000);
 };
 socket.onclose=()=>{
  clearInterval(heartbeatTimer);
  if(ws===socket)ws=null;
  $("connection").textContent="Reconnecting…";
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
  const base=location.origin;let html=`<h2>Room created: ${d.room_code}</h2><p>Save these links now. The raw invite tokens are only shown once.</p><p><b>Host link:</b> <a href="${base}${d.host_url}&host=1">${base}${d.host_url}&host=1</a></p><h3>Franchise invites</h3>`;
  d.franchise_invites.forEach(x=>html+=`<p><b>${esc(x.franchise)}:</b> <a href="${base}${x.url}">${base}${x.url}</a></p>`);
  html+=`<p>Players loaded: ${d.players_loaded}. Copy the host link and franchise links somewhere safe.</p>`;
  $("invite-output").innerHTML=html;show("invite-output");hide("landing");
  roomCode=d.room_code;token=d.host_token;isHost=true;history.replaceState({}, "", `/?room=${roomCode}&token=${token}&host=1`);await init();
 }catch(err){alert(err.message)}
});
$("copy-link").onclick=()=>navigator.clipboard.writeText(location.href);
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
  default:return d.detail||"Bid rejected";
 }
}
$("start-auction").onclick=async()=>{if(!confirm("Lock the player pool and start the auction?"))return;try{const d=await api(`/api/rooms/${roomCode}/start`,{method:"POST"});render(d);show("live-area");hide("host-setup");}catch(e){alert(e.message)}};
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
  $("role-label").textContent=myFranchise?` · Host / ${myFranchise.name}`:" · Administrator";
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
