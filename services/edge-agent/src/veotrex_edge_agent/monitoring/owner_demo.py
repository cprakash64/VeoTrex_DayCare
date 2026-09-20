"""Self-contained owner-facing demo page served by the demo runtime.

No Node, no bundler, no API server, no database, no network. One HTML string with inline CSS
and vanilla JS that polls this same process for state. Every number on the page comes from
the running pipeline; absence renders as an em dash, never as a zero.
"""

from __future__ import annotations

OWNER_DEMO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VeoTrex - Safety Operations</title>
<style>
*{box-sizing:border-box}
:root{
  --bg:#071613; --surface:#0b1f1a; --surface-2:#0e2620; --line:#1d4438;
  --text:#e8f4ef; --muted:#93b3a6; --faint:#6d8d81;
  --ok:#6de0b2; --warn:#f2c57c; --unknown:#8fa8bd;
}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1400px;margin:0 auto;padding:22px 26px 34px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;
  flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.mark{width:30px;height:30px;border-radius:8px;background:var(--ok);color:#06241b;
  display:grid;place-items:center;font-weight:800;font-size:.9rem}
.brandname{font-weight:700;letter-spacing:.02em;font-size:1.05rem}
h1{margin:0;font-size:1.65rem;line-height:1.15;letter-spacing:-.015em}
.sub{margin:4px 0 0;color:var(--muted);font-size:.95rem}
.head-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:7px;padding:.3rem .65rem;border-radius:999px;
  font-size:.7rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
  border:1px solid transparent;white-space:nowrap}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.b-rec{background:rgba(242,197,124,.13);color:var(--warn);border-color:rgba(242,197,124,.42)}
.b-ok{background:rgba(109,224,178,.12);color:var(--ok);border-color:rgba(109,224,178,.34)}
.b-warn{background:rgba(242,197,124,.12);color:var(--warn);border-color:rgba(242,197,124,.34)}
.b-unk{background:rgba(143,168,189,.12);color:var(--unknown);border-color:rgba(143,168,189,.32)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;align-items:start}
.card{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:16px 17px}
.card.flush{padding:0;overflow:hidden}
.card h2{margin:0 0 12px;font-size:.72rem;font-weight:700;letter-spacing:.11em;
  text-transform:uppercase;color:var(--faint)}
.stage{position:relative;aspect-ratio:16/9;background:#040d0b}
.stage img{width:100%;height:100%;object-fit:contain;display:block}
.stage .veil{position:absolute;inset:0;display:flex;flex-direction:column;
  justify-content:space-between;padding:14px 16px;pointer-events:none}
.veil .row{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.veil .row.end{justify-content:flex-end}
.vtitle{font-weight:600;font-size:1rem;text-shadow:0 1px 8px rgba(0,0,0,.9)}
.chip{background:rgba(4,13,11,.72);border:1px solid rgba(232,244,239,.18);color:var(--text);
  padding:.26rem .6rem;border-radius:999px;font-size:.7rem;font-weight:600;
  font-variant-numeric:tabular-nums;letter-spacing:.04em}
.offline{position:absolute;inset:0;display:grid;place-content:center;text-align:center;
  gap:8px;padding:30px;color:var(--muted)}
.offline strong{color:var(--text);font-size:1.05rem}
.side{display:flex;flex-direction:column;gap:16px}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
.kpi{background:var(--surface-2);padding:13px 14px}
.kpi .k{color:var(--muted);font-size:.7rem;font-weight:600;letter-spacing:.09em;
  text-transform:uppercase}
.kpi .v{margin-top:5px;font-size:1.9rem;font-weight:650;font-variant-numeric:tabular-nums;
  line-height:1.05}
.kpi .v.unk{font-size:1.5rem;color:var(--unknown)}
.rows{display:flex;flex-direction:column;gap:11px}
.row2{display:flex;align-items:center;justify-content:space-between;gap:12px}
.row2 .k{color:var(--muted);font-size:.88rem}
.meta{display:flex;gap:16px;flex-wrap:wrap;margin-top:13px;padding-top:12px;
  border-top:1px solid var(--line);color:var(--faint);font-size:.76rem;
  font-variant-numeric:tabular-nums}
.act{list-style:none;margin:0;padding:0;max-height:212px;overflow-y:auto}
.act li{display:grid;grid-template-columns:70px minmax(0,1fr);gap:12px;padding:9px 0;
  border-top:1px solid var(--line)}
.act li:first-child{border-top:0;padding-top:0}
.t{color:var(--faint);font-size:.78rem;font-variant-numeric:tabular-nums}
.ti{font-weight:600;font-size:.9rem}
.tm{color:var(--muted);font-size:.8rem;margin-top:2px}
.empty{color:var(--muted);font-size:.9rem}
.empty strong{display:block;color:var(--text);margin-bottom:3px}
.note{margin:13px 0 0;padding:9px 11px;border-radius:8px;font-size:.78rem;line-height:1.5;
  color:var(--muted);background:rgba(143,168,189,.06);border:1px solid var(--line)}
@media(max-width:1180px){.grid{grid-template-columns:minmax(0,1fr)}}
</style></head>
<body><div class="wrap">
<header>
  <div>
    <div class="brand"><span class="mark">V</span><span class="brandname">VeoTrex</span></div>
    <h1>Safety Operations</h1>
    <p class="sub">AI-assisted classroom monitoring</p>
  </div>
  <div class="head-right">
    <span class="badge b-rec"><span class="dot"></span>Recorded Demo</span>
    <span class="badge b-ok" id="sysbadge"><span class="dot"></span>Monitoring Active</span>
  </div>
</header>

<div class="grid">
  <div>
    <section class="card flush">
      <div class="stage">
        <img id="stream" src="/stream.mjpg" alt="Monitored classroom with detected people outlined">
        <div class="veil">
          <div class="row">
            <span class="vtitle" id="area">Demo Classroom</span>
            <span class="badge b-rec"><span class="dot"></span>Recorded Demo</span>
          </div>
          <div class="row end">
            <span class="chip" id="c-fps">-- fps</span>
            <span class="chip" id="c-lat">-- ms</span>
            <span class="chip" id="c-trk">-- tracks</span>
          </div>
        </div>
        <div class="offline" id="offline" style="display:none">
          <strong>Monitoring source unavailable</strong>
          <span>Safety state is unknown. Loss of video is never reported as an empty room.</span>
        </div>
      </div>
    </section>

    <section class="card" style="margin-top:16px">
      <h2>Recent activity</h2>
      <ul class="act" id="acts"></ul>
      <div class="empty" id="acts-empty">
        <strong>No activity yet this session.</strong>
        Entries appear as the system observes people entering and leaving the monitored view.
      </div>
    </section>
  </div>

  <div class="side">
    <section class="card">
      <h2>Classroom status</h2>
      <div class="kpis">
        <div class="kpi"><div class="k">Current occupancy</div>
          <div class="v" id="occ">--</div></div>
        <div class="kpi"><div class="k">Active tracks</div><div class="v" id="act-n">--</div></div>
        <div class="kpi"><div class="k">Peak occupancy</div><div class="v" id="peak">--</div></div>
        <div class="kpi"><div class="k">Tracks observed</div><div class="v" id="seen">--</div></div>
      </div>
      <p class="note">Occupancy is a count of distinct people currently tracked. VeoTrex does
      not estimate age, identity or role from imagery.</p>
    </section>

    <section class="card">
      <h2>System</h2>
      <div class="rows">
        <div class="row2"><span class="k">AI analysis</span>
          <span class="badge b-ok" id="s-ai">Active</span></div>
        <div class="row2"><span class="k">Tracking</span>
          <span class="badge b-ok" id="s-tr">Active</span></div>
        <div class="row2"><span class="k">Monitoring coverage</span>
          <span class="badge b-ok" id="s-cov">Active</span></div>
      </div>
      <div class="meta">
        <span>Session <b id="m-ses">--</b></span>
        <span>Longest track <b id="m-long">--</b></span>
        <span>Frames <b id="m-fr">--</b></span>
      </div>
      <p class="note">Video is decoded and analysed on this device. Frames are not sent to a
      cloud service.</p>
    </section>
  </div>
</div>
</div>
<script>
var TITLES = {
  TRACK_STARTED: "Person entered monitored view",
  TRACK_ENDED: "Person left monitored view",
  OCCUPANCY_CHANGED: "Occupancy changed",
  PEAK_OCCUPANCY: "New peak occupancy",
  MONITORING_COVERAGE_LOST: "Monitoring coverage lost",
  MONITORING_COVERAGE_RESTORED: "Monitoring coverage restored",
  DEMO_THRESHOLD_EXCEEDED: "Occupancy threshold exceeded",
  DEMO_THRESHOLD_CLEARED: "Occupancy threshold cleared"
};
function el(id){return document.getElementById(id)}
function txt(id,v){el(id).textContent=v}
// Absence renders as an em dash. Never as zero, never as a plausible placeholder.
function num(v){return (v===null||v===undefined)?"\\u2014":String(v)}
function badge(id,label,cls){var n=el(id);n.textContent=label;n.className="badge "+cls}
function clock(iso){var d=new Date(iso);return isNaN(d.getTime())?"\\u2014":
  d.toLocaleTimeString([], {hour:"numeric",minute:"2-digit",second:"2-digit"})}
function mmss(s){if(s===null||s===undefined)return "\\u2014";
  var t=Math.floor(s);return Math.floor(t/60)+":"+String(t%60).padStart(2,"0")}

function unknownState(){
  txt("occ","\\u2014"); el("occ").className="v unk";
  txt("act-n","\\u2014"); el("act-n").className="v unk";
  badge("s-cov","Unknown","b-unk");
  badge("s-ai","Unknown","b-unk");
  badge("s-tr","Unknown","b-unk");
  badge("sysbadge","Monitoring Unavailable","b-unk");
  el("offline").style.display="grid";
  txt("c-fps","\\u2014 fps"); txt("c-lat","\\u2014 ms"); txt("c-trk","\\u2014 tracks");
}

function render(s){
  var cov=s.coverage, measured=(s.occupancy!==null&&s.occupancy!==undefined);
  el("offline").style.display = (cov==="ACTIVE")?"none":"grid";
  el("area").textContent = s.area || "Demo Classroom";

  if(measured){ txt("occ",String(s.occupancy)); el("occ").className="v"; }
  else { txt("occ","\\u2014"); el("occ").className="v unk"; }
  if(s.active_tracks!==null&&s.active_tracks!==undefined){
    txt("act-n",String(s.active_tracks)); el("act-n").className="v";
  } else { txt("act-n","\\u2014"); el("act-n").className="v unk"; }
  txt("peak",num(s.peak_occupancy));
  txt("seen",num(s.tracks_observed));

  if(cov==="ACTIVE"){ badge("s-cov","Active","b-ok");
    badge("sysbadge","Monitoring Active","b-ok"); }
  else if(cov==="IMPAIRED"){ badge("s-cov","Impaired","b-warn");
    badge("sysbadge","Coverage Impaired","b-warn"); }
  else { badge("s-cov","Unknown","b-unk"); badge("sysbadge","Monitoring Unavailable","b-unk"); }
  var running = (cov==="ACTIVE");
  badge("s-ai", running?"Active":"Unknown", running?"b-ok":"b-unk");
  badge("s-tr", running?"Active":"Unknown", running?"b-ok":"b-unk");

  txt("c-fps", num(s.fps)+" fps");
  txt("c-lat", num(s.inference_latency_ms)+" ms");
  txt("c-trk", num(s.active_tracks)+" tracks");
  txt("m-ses", mmss(s.session_seconds));
  txt("m-long", s.longest_track_seconds===null||s.longest_track_seconds===undefined
    ? "\\u2014" : s.longest_track_seconds+"s");
  txt("m-fr", num(s.frames_processed));

  var evs=s.events||[], list=el("acts");
  el("acts-empty").style.display = evs.length?"none":"block";
  list.innerHTML="";
  evs.slice(0,40).forEach(function(e){
    var li=document.createElement("li");
    var t=document.createElement("span"); t.className="t"; t.textContent=clock(e.occurred_at);
    var b=document.createElement("span");
    var i=document.createElement("span"); i.className="ti";
    i.textContent=TITLES[e.kind]||String(e.kind).replace(/_/g," ").toLowerCase();
    var m=document.createElement("span"); m.className="tm";
    var bits=[e.area_label];
    if(e.people_detected!==null&&e.people_detected!==undefined)
      bits.push(e.people_detected+" in view");
    if(e.duration_seconds!==null&&e.duration_seconds!==undefined)
      bits.push(e.duration_seconds+"s");
    m.textContent=bits.join(" \\u00b7 ");
    b.appendChild(i); b.appendChild(m); li.appendChild(t); li.appendChild(b);
    list.appendChild(li);
  });
}

function poll(){
  fetch("/api/demo/status",{cache:"no-store"})
    .then(function(r){ if(!r.ok) throw 0; return r.json(); })
    .then(render)
    .catch(unknownState);
}
poll(); setInterval(poll, 750);
// A stalled MJPEG connection should retry rather than sit on a frozen last frame.
el("stream").addEventListener("error", function(){
  var img=el("stream");
  setTimeout(function(){ img.src="/stream.mjpg?r="+Date.now(); }, 1500);
});
</script></body></html>
"""
