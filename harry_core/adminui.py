"""HTTP surface for the administrator control panel.

Every route under ``/api/admin/`` verifies the elevation token on the server
before doing anything. The browser cannot turn itself into an administrator by
hiding or showing a button: the token only exists after a password (and TOTP
when enrolled) is verified, it is stored in a signed session cookie, and it is
bound to the signed-in account.

The panel itself (``/admin``) is just a client; it holds no authority.
"""

import functools
import json
import logging
import os
import re
import secrets
import time

from . import admin, capabilities, config, monitoring, selfdev, selfheal

log = logging.getLogger("harry.adminui")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


# ------------------------------------------------------------------ page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Admin · Harry</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<style>
  :root{color-scheme:dark;--bg:#080f1b;--card:#111b2c;--line:rgba(154,180,218,.16);
    --text:#e9effb;--dim:#93a5c1;--accent:#6384f3;--warn:#f0b45c;--bad:#ff7887;--ok:#5fd6a4;}
  *{box-sizing:border-box}body{margin:0;font:14px/1.55 Inter,"Segoe UI",system-ui,sans-serif;
    background:radial-gradient(ellipse at 12% 0%,rgba(94,126,226,.16),transparent 46%),var(--bg);color:var(--text)}
  header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line)}
  header h1{font-size:13px;letter-spacing:.22em;margin:0}header .sp{flex:1}
  .pill{padding:3px 9px;border-radius:99px;font-size:11px;border:1px solid var(--line);color:var(--dim)}
  .pill.on{color:var(--ok);border-color:rgba(95,214,164,.4);background:rgba(95,214,164,.08)}
  .pill.off{color:var(--warn);border-color:rgba(240,180,92,.35)}
  main{max-width:1120px;margin:0 auto;padding:22px 20px 60px}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
  .card h2{margin:0 0 10px;font-size:12px;letter-spacing:.12em;color:var(--dim);text-transform:uppercase}
  .big{font-size:26px;font-weight:650}.muted{color:var(--dim);font-size:12px}
  button{font:inherit;border-radius:10px;border:1px solid var(--line);background:#16233a;
    color:var(--text);padding:8px 12px;cursor:pointer}button.primary{background:var(--accent);border-color:transparent;color:#fff;font-weight:650}
  button.danger{border-color:rgba(255,120,135,.4);color:var(--bad)}
  button:disabled{opacity:.45;cursor:not-allowed}
  input,select{font:inherit;width:100%;padding:9px 11px;border-radius:9px;border:1px solid var(--line);
    background:#07101d;color:inherit}
  label{display:block;font-size:11px;color:var(--dim);margin:10px 0 4px;text-transform:uppercase;letter-spacing:.06em}
  table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid rgba(154,180,218,.09);vertical-align:top}
  th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  .ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
  pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,Consolas,monospace;color:#c9d6ea}
  .err{margin-top:10px;padding:9px 11px;border-radius:9px;background:rgba(204,64,87,.14);
    color:var(--bad);border:1px solid rgba(204,64,87,.3);font-size:12px}
  .banner{margin:0 0 16px;padding:10px 13px;border-radius:11px;font-size:12.5px;
    border:1px solid rgba(240,180,92,.35);background:rgba(240,180,92,.08);color:var(--warn)}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 14px}
  .tabs button{background:transparent}.tabs button.active{background:#16233a;border-color:var(--accent);color:#dbe4f7}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  section[hidden]{display:none}
</style></head>
<body>
<header>
  <h1>HARRY · ADMIN</h1>
  <span id="mode" class="pill off">not elevated</span>
  <span class="sp"></span>
  <button onclick="location.href='/'">Back to Harry</button>
  <button id="exitBtn" class="danger" hidden onclick="exitAdmin()">Exit admin mode</button>
</header>
<main>
  <div id="banner" class="banner" hidden></div>

  <div class="card" id="loginCard">
    <h2>Administrator authentication</h2>
    <p class="muted">Step-up authentication is required. Re-enter the administrator password
      (and authenticator code if enrolled). A signed-in session alone is not enough.</p>
    <label for="email">Email (only needed if several admins exist)</label>
    <input id="email" type="email" autocomplete="username" placeholder="admin@example.com">
    <label for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password">
    <label for="code">Authenticator code (if enabled)</label>
    <input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="123456">
    <div class="err" id="loginErr" hidden></div>
    <div class="row" style="margin-top:14px"><button class="primary" onclick="enterAdmin()">Enter admin mode</button></div>
    <div id="setupBox" hidden style="margin-top:18px;padding-top:16px;border-top:1px solid var(--line)">
      <h2 style="margin-top:0">First-run setup</h2>
      <p class="muted">No account exists on this installation. The first account becomes the
        administrator. This is allowed only from this computer (loopback).</p>
      <label for="setupEmail">Email</label><input id="setupEmail" type="email" autocomplete="username">
      <label for="setupPw">Password (12+ characters)</label><input id="setupPw" type="password" autocomplete="new-password">
      <div class="err" id="setupErr" hidden></div>
      <div class="row" style="margin-top:12px"><button class="primary" onclick="bootstrapAdmin()">Create administrator</button></div>
    </div>
  </div>

  <div id="panel" hidden>
    <div class="grid" id="tiles"></div>
    <div class="tabs">
      <button data-tab="health" class="active">Health</button>
      <button data-tab="capabilities">Capabilities</button>
      <button data-tab="plugins">Plugins</button>
      <button data-tab="upgrades">Upgrades</button>
      <button data-tab="approvals">Approvals</button>
      <button data-tab="audit">Audit</button>
      <button data-tab="sessions">Sessions</button>
      <button data-tab="security">Security</button>
    </div>

    <section id="tab-health">
      <div class="row" style="margin-bottom:12px">
        <button onclick="runDiagnostics()">Run full diagnostic</button>
        <button onclick="healAll()">Repair healthy-repairable components</button>
        <button onclick="loadHealth()">Refresh</button>
      </div>
      <div class="card"><h2>Component health</h2><div id="health"></div></div>
      <div class="card" style="margin-top:14px"><h2>Alerts</h2><div id="alerts"></div></div>
      <div class="card" style="margin-top:14px"><h2>Error reports</h2><div id="errors"></div></div>
      <div class="card" style="margin-top:14px"><h2>Diagnostic output</h2><pre id="diag"></pre></div>
    </section>

    <section id="tab-capabilities" hidden>
      <div class="card"><h2>Capability registry</h2><div class="muted" id="capCount"></div><div id="caps"></div></div>
    </section>

    <section id="tab-plugins" hidden>
      <div class="card"><h2>Plugins on disk</h2>
        <p class="muted">A plugin is imported only after its manifest, SHA-256 and static
          security scan pass, and an administrator approves it.</p>
        <div id="plugins"></div></div>
    </section>

    <section id="tab-upgrades" hidden>
      <div class="card"><h2>Propose a capability</h2>
        <label for="upReq">What should Harry be able to do?</label>
        <input id="upReq" placeholder="convert a distance between km and miles">
        <label for="upName">Plugin name (optional)</label>
        <input id="upName" placeholder="tool_convert_distance">
        <div class="row" style="margin-top:12px">
          <button class="primary" onclick="proposeUpgrade()">Generate and stage</button>
        </div>
        <div class="err" id="upErr" hidden></div>
      </div>
      <div class="card" style="margin-top:14px"><h2>Upgrade history</h2><div id="upgrades"></div></div>
      <div class="card" style="margin-top:14px"><h2>Backups</h2><div id="backups"></div></div>
    </section>

    <section id="tab-approvals" hidden>
      <div class="card"><h2>Pending approvals</h2>
        <p class="muted">High-risk actions wait here. Nothing is executed by the request that asked.</p>
        <div id="approvals"></div></div>
    </section>

    <section id="tab-audit" hidden>
      <div class="card"><h2>Audit history</h2><div id="audit"></div></div>
    </section>

    <section id="tab-sessions" hidden>
      <div class="card"><h2>Elevated sessions</h2><div id="sessions"></div></div>
    </section>

    <section id="tab-security" hidden>
      <div class="card"><h2>Multi-factor authentication</h2>
        <p class="muted" id="mfaState"></p>
        <div class="row">
          <button onclick="enrollMfa()">Enroll authenticator</button>
          <button class="danger" onclick="disableMfa()">Disable MFA</button>
        </div>
        <div id="mfaBox" style="margin-top:12px" hidden>
          <p class="muted">Add this secret to an authenticator app, then confirm a code.</p>
          <pre id="mfaSecret"></pre>
          <div class="row"><input id="mfaCode" inputmode="numeric" placeholder="123456" style="max-width:160px">
          <button class="primary" onclick="confirmMfa()">Confirm</button></div>
        </div>
      </div>
      <div class="card" style="margin-top:14px"><h2>Accounts and roles</h2>
        <div id="users"></div>
      </div>
      <div class="card" style="margin-top:14px"><h2>Policy</h2>
        <p class="muted">Security, authentication and permission changes are never applied
          automatically: they require an approval record that a second action must consume.</p>
        <pre id="policy"></pre>
      </div>
    </section>
  </div>
</main>
<script>
let ADMIN=false, CSRF='';
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts={}){
  const init={method:opts.method||'GET',headers:{},credentials:'same-origin'};
  if(opts.body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(opts.body);}
  if(init.method!=='GET'&&CSRF){init.headers['X-CSRF-Token']=CSRF;init.headers['X-Admin-CSRF']=CSRF;}
  const res=await fetch(path,init);
  let data={};try{data=await res.json();}catch(_){}
  if(!res.ok){const e=new Error(data.error||('HTTP '+res.status));e.status=res.status;e.data=data;throw e;}
  return data;
}

function showError(id,msg){const el=$(id);if(!el)return;el.hidden=!msg;el.textContent=msg||'';}

async function checkSession(){
  try{
    const s=await api('/api/admin/session');
    CSRF=s.csrf||'';
    ADMIN=!!s.admin;
    $('mode').textContent=ADMIN?('admin · '+ (s.email||'')):'not elevated';
    $('mode').className='pill '+(ADMIN?'on':'off');
    $('loginCard').hidden=ADMIN;$('panel').hidden=!ADMIN;$('exitBtn').hidden=!ADMIN;
    $('setupBox').hidden = !(s && s.needs_setup);
    if(ADMIN){
      $('banner').hidden=true;
      $('mfaState').textContent=s.mfa?'MFA is enabled for this account.':'MFA is not enabled. Enroll an authenticator for a second factor.';
      loadHealth();loadCaps();loadPlugins();loadUpgrades();loadApprovals();loadAudit();loadSessions();loadPolicy();loadUsers();
    }else{
      $('banner').hidden=false;
      $('banner').textContent='Administrator mode is inactive. Authenticate below; natural-language claims of being the admin have no effect.';
    }
  }catch(e){$('mode').textContent='not signed in';$('mode').className='pill off';}
}

async function enterAdmin(){
  showError('loginErr','');
  try{
    const body={password:$('pw').value,code:$('code').value,email:$('email').value};
    const r=await api('/api/admin/enter',{method:'POST',body});
    CSRF=r.csrf||CSRF;$('pw').value='';$('code').value='';
    await checkSession();
  }catch(e){showError('loginErr',e.message);}
}
async function bootstrapAdmin(){
  showError('setupErr','');
  try{
    await api('/api/admin/bootstrap',{method:'POST',body:{email:$('setupEmail').value,password:$('setupPw').value}});
    $('email').value=$('setupEmail').value;
    $('setupPw').value='';
    $('setupBox').hidden=true;
    alert('Administrator created. Now enter the password below to elevate.');
    checkSession();
  }catch(e){showError('setupErr',e.message);}
}
async function exitAdmin(){
  try{await api('/api/admin/exit',{method:'POST',body:{}});}catch(_){}
  ADMIN=false;await checkSession();
}

function tile(label,value,tone){return `<div class="card"><h2>${esc(label)}</h2><div class="big ${tone||''}">${esc(value)}</div></div>`;}

async function loadHealth(){
  try{
    const h=await api('/api/admin/health');
    const t=h.tiles||{};
    $('tiles').innerHTML=[tile('Backend',t.backend,t.backendOk?'ok':'bad'),
      tile('AI model',t.model,t.modelOk?'ok':'bad'),tile('Voice',t.voice,t.voiceOk?'ok':'warn'),
      tile('Disk free',t.diskFree,t.diskOk?'':'bad'),tile('Elevated sessions',t.sessions),
      tile('Unhealthy',t.unhealthy,(t.unhealthy==='0')?'ok':'warn')].join('');
    const rows=Object.entries(h.checks||{}).map(([k,v])=>{
      const ok=v.ok!==false;
      return `<tr><td>${esc(k)}</td><td class="${ok?'ok':'bad'}">${ok?'ok':'failed'}</td>
        <td class="muted">${esc(JSON.stringify(v).slice(0,180))}</td></tr>`;}).join('');
    $('health').innerHTML=`<table><tr><th>Check</th><th>State</th><th>Detail</th></tr>${rows}</table>`;
    const alerts=h.alerts||[];
    $('alerts').innerHTML=alerts.length?`<table><tr><th>When</th><th>Kind</th><th>Message</th></tr>${
      alerts.map(a=>`<tr><td class="muted">${new Date(a.at*1000).toLocaleString()}</td>
      <td>${esc(a.kind)}</td><td>${esc(a.message)}</td></tr>`).join('')}</table>`:'<p class="muted">No alerts.</p>';
    loadErrors();
  }catch(e){$('health').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}

async function runDiagnostics(){
  $('diag').textContent='running…';
  try{const r=await api('/api/admin/diagnostics',{method:'POST',body:{}});
    $('diag').textContent=r.report;}catch(e){$('diag').textContent='failed: '+e.message;}
  loadHealth();
}
async function healAll(){
  try{const r=await api('/api/admin/heal',{method:'POST',body:{component:'all'}});
    $('diag').textContent=JSON.stringify(r,null,2);}catch(e){$('diag').textContent='failed: '+e.message;}
  loadHealth();
}

async function loadCaps(){
  try{
    const r=await api('/api/admin/capabilities');
    $('capCount').textContent=`${r.items.length} capabilities · ${r.plugins} from plugins · ${r.failed} failing`;
    $('caps').innerHTML=`<table><tr><th>Name</th><th>Version</th><th>Source</th><th>Status</th><th>Tests</th><th></th></tr>${
      r.items.map(c=>`<tr><td>${esc(c.name)}</td><td class="muted">${esc(c.version)}</td>
        <td class="muted">${esc(c.source)}</td><td class="${c.status==='active'?'ok':'warn'}">${esc(c.status)}</td>
        <td class="muted">${c.tests?esc(c.tests.passed)+' / '+esc(c.tests.failed):'-'}</td>
        <td><button onclick="testCap('${esc(c.name)}')">Test</button></td></tr>`).join('')}</table>`;
  }catch(e){$('caps').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function testCap(name){
  try{const r=await api('/api/admin/capabilities/'+encodeURIComponent(name)+'/test',{method:'POST',body:{}});
    alert(name+': '+(r.ok?'passed':'failed')+'\n'+JSON.stringify(r.result||{},null,2).slice(0,900));}
  catch(e){alert('failed: '+e.message);}
  loadCaps();
}

async function loadPlugins(){
  try{
    const r=await api('/api/admin/plugins');
    if(!r.items.length){$('plugins').innerHTML='<p class="muted">No plugins installed.</p>';return;}
    $('plugins').innerHTML=`<table><tr><th>Plugin</th><th>Version</th><th>State</th><th>Problems</th><th></th></tr>${
      r.items.map(p=>`<tr><td>${esc(p.id)}</td><td class="muted">${esc(p.version||'')}</td>
      <td class="${p.valid?'ok':'bad'}">${esc(p.status||(p.valid?'valid':'invalid'))}</td>
      <td class="muted">${esc((p.problems||[]).join('; '))}</td>
      <td>${p.valid&&p.status!=='active'?`<button class="primary" onclick="approvePlugin('${esc(p.id)}')">Approve &amp; activate</button>`:''}</td></tr>`).join('')}</table>`;
  }catch(e){$('plugins').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function approvePlugin(id){
  try{await api('/api/admin/plugins/'+encodeURIComponent(id)+'/approve',{method:'POST',body:{}});}
  catch(e){alert('failed: '+e.message);}
  loadPlugins();loadCaps();
}

async function proposeUpgrade(){
  showError('upErr','');$('upErr').hidden=true;
  try{
    const body={request:$('upReq').value,name:$('upName').value||undefined};
    const r=await api('/api/admin/upgrades/propose',{method:'POST',body});
    if(r.needs_approval){$('upErr').hidden=false;$('upErr').textContent=
      'Staged. It now waits in Approvals; nothing runs until you approve it.';}
  }catch(e){$('upErr').hidden=false;$('upErr').textContent=e.message;}
  loadUpgrades();loadApprovals();
}
async function loadUpgrades(){
  try{
    const r=await api('/api/admin/upgrades');
    $('upgrades').innerHTML=r.items.length?`<table><tr><th>When</th><th>Name</th><th>Status</th><th>Notes</th><th></th></tr>${
      r.items.map(u=>`<tr><td class="muted">${new Date(u.created*1000).toLocaleString()}</td>
      <td>${esc(u.name)}</td><td class="${u.status==='active'?'ok':(u.status==='failed'?'bad':'warn')}">${esc(u.status)}</td>
      <td class="muted">${esc((u.problems||[]).join('; ').slice(0,120))}</td>
      <td><button onclick="requestApproval('${esc(u.id)}')">Request approval</button>
      ${u.status==='active'?`<button class="danger" onclick="rollback('${esc(u.id)}')">Roll back</button>`:''}</td></tr>`).join('')}</table>`
      :'<p class="muted">No upgrades yet.</p>';
    const b=await api('/api/admin/backups');
    $('backups').innerHTML=b.items.length?`<table><tr><th>Created</th><th>Label</th><th>Files</th></tr>${
      b.items.slice(0,10).map(x=>`<tr><td class="muted">${new Date(x.created*1000).toLocaleString()}</td>
      <td>${esc(x.label)}</td><td class="muted">${Object.keys(x.files||{}).length}</td></tr>`).join('')}</table>`
      :'<p class="muted">No checkpoints.</p>';
  }catch(e){$('upgrades').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function requestApproval(id){
  try{await api('/api/admin/upgrades/'+encodeURIComponent(id)+'/request-approval',{method:'POST',body:{}});}
  catch(e){alert(e.message);}
  loadUpgrades();loadApprovals();
}
async function rollback(id){
  if(!confirm('Restore the checkpoint taken before this upgrade?'))return;
  try{const r=await api('/api/admin/upgrades/'+encodeURIComponent(id)+'/rollback',{method:'POST',body:{}});
    alert('rolled back: '+(r.restored||0)+' files restored');}
  catch(e){alert('failed: '+e.message);}
  loadUpgrades();loadCaps();
}

async function loadApprovals(){
  try{
    const r=await api('/api/admin/approvals');
    $('approvals').innerHTML=r.items.length?`<table><tr><th>When</th><th>Action</th><th>Risk</th><th>Status</th><th>Payload</th><th></th></tr>${
      r.items.map(a=>`<tr><td class="muted">${new Date(a.created*1000).toLocaleString()}</td>
      <td>${esc(a.action)}</td><td class="warn">${esc(a.risk)}</td><td>${esc(a.status)}</td>
      <td class="muted">${esc(JSON.stringify(a.payload).slice(0,120))}</td>
      <td>${a.status==='pending'?`<button class="primary" onclick="decide('${esc(a.id)}',true)">Approve</button>
      <button class="danger" onclick="decide('${esc(a.id)}',false)">Deny</button>`:''}</td></tr>`).join('')}</table>`
      :'<p class="muted">Nothing awaiting approval.</p>';
  }catch(e){$('approvals').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function decide(id,approve){
  try{await api('/api/admin/approvals/'+encodeURIComponent(id),{method:'POST',body:{approve}});}
  catch(e){alert(e.message);}
  loadApprovals();loadUpgrades();
}

async function loadAudit(){
  try{
    const r=await api('/api/admin/audit');
    $('audit').innerHTML=`<table><tr><th>When</th><th>Action</th><th>Detail</th></tr>${
      r.items.map(a=>`<tr><td class="muted">${new Date(a.ts*1000).toLocaleString()}</td>
      <td>${esc(a.action)}</td><td class="muted">${esc(JSON.stringify(a.detail).slice(0,160))}</td></tr>`).join('')}</table>`;
  }catch(e){$('audit').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}

async function loadSessions(){
  try{
    const r=await api('/api/admin/sessions');
    $('sessions').innerHTML=`<table><tr><th>Session</th><th>Started</th><th>Last used</th><th>State</th><th></th></tr>${
      r.items.map(s=>`<tr><td class="muted">${esc(s.id)}</td>
      <td class="muted">${new Date(s.created*1000).toLocaleString()}</td>
      <td class="muted">${new Date(s.last_seen*1000).toLocaleString()}</td>
      <td class="${s.active?'ok':'warn'}">${s.active?'active':'expired'}</td>
      <td>${s.active?`<button class="danger" onclick="revoke('${esc(s.id)}')">Revoke</button>`:''}</td></tr>`).join('')}</table>`;
  }catch(e){$('sessions').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function revoke(id){
  try{await api('/api/admin/sessions/'+encodeURIComponent(id)+'/revoke',{method:'POST',body:{}});}
  catch(e){alert(e.message);}
  loadSessions();checkSession();
}

async function loadErrors(){
  try{
    const r=await api('/api/admin/errors');
    const rows=(r.history||[]).filter(h=>h.kind==='error'||h.kind==='repair').slice(0,12);
    const jobs=(r.failed_jobs||[]).slice(0,6);
    $('errors').innerHTML=(rows.length||jobs.length)?`<table><tr><th>When</th><th>Source</th><th>Detail</th></tr>${
      rows.map(h=>`<tr><td class="muted">${new Date(h.at*1000).toLocaleString()}</td>
      <td>${esc(h.component||h.kind)}</td><td class="muted">${esc((h.error||(h.result&&JSON.stringify(h.result))||'').toString().slice(0,140))}</td></tr>`).join('')
      +jobs.map(j=>`<tr><td class="muted">${new Date((j.created||0)*1000).toLocaleString()}</td>
      <td>job: ${esc(j.label||j.kind)}</td><td class="muted">${esc((j.error||'').slice(0,140))}</td></tr>`).join('')}</table>`
      :'<p class="muted">No errors recorded.</p>';
  }catch(e){$('errors').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}

async function loadUsers(){
  try{
    const r=await api('/api/admin/users');
    $('users').innerHTML=`<table><tr><th>Account</th><th>Role</th><th></th></tr>${
      r.items.map(u=>`<tr><td>${esc(u.email)}<div class="muted">${esc(u.id)}</div></td>
      <td class="${u.administrator?'ok':'muted'}">${esc(u.role)}</td>
      <td>${u.administrator?'':`<button onclick="setRole('${esc(u.id)}','admin')">Make administrator</button>`}
      ${u.administrator?`<button onclick="setRole('${esc(u.id)}','user')">Remove administrator</button>`:''}</td></tr>`).join('')}</table>`;
  }catch(e){$('users').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
}
async function setRole(id, role){
  try{
    await api('/api/admin/users/'+encodeURIComponent(id)+'/role',{method:'POST',body:{role}});
    alert('Role changed.');
  }catch(e){
    if(e.data&&e.data.approval){
      alert('Approval required. Approve '+e.data.approval+' in the Approvals tab, then press the same button again.');
      loadApprovals();
    } else { alert(e.message); }
  }
  loadUsers();
}

async function loadPolicy(){
  try{const r=await api('/api/admin/policy');$('policy').textContent=JSON.stringify(r,null,2);}
  catch(e){$('policy').textContent=e.message;}
}
async function enrollMfa(){
  try{const r=await api('/api/admin/mfa/enroll',{method:'POST',body:{}});
    $('mfaBox').hidden=false;$('mfaSecret').textContent=r.otpauth;}
  catch(e){alert(e.message);}
}
async function confirmMfa(){
  try{await api('/api/admin/mfa/confirm',{method:'POST',body:{code:$('mfaCode').value}});
    $('mfaBox').hidden=true;alert('MFA enabled');checkSession();}
  catch(e){alert(e.message);}
}
async function disableMfa(){
  if(!confirm('Disable the second factor for this account?'))return;
  try{await api('/api/admin/mfa/disable',{method:'POST',body:{}});checkSession();}
  catch(e){alert(e.message);}
}

document.querySelectorAll('.tabs button').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b===btn));
    document.querySelectorAll('section[id^=tab-]').forEach(s=>s.hidden=s.id!=='tab-'+btn.dataset.tab);
  };
});
$('pw').addEventListener('keydown',e=>{if(e.key==='Enter')enterAdmin();});
checkSession();
</script>
</body></html>"""


# ------------------------------------------------------------------ helpers

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
FORWARDED_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                     "X-Forwarded-Port", "Forwarded", "X-Real-IP",
                     "CF-Connecting-IP", "True-Client-IP")


def _is_loopback_ip(value):
    value = str(value or "").strip()
    return value.startswith("127.") or value in ("::1", "0:0:0:0:0:0:0:1")


def _host_name(host):
    """Lower-case host without its port, keeping IPv6 brackets."""
    host = str(host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _trusted_proxies():
    raw = os.environ.get("HARRY_TRUSTED_PROXIES", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _client_ip():
    from flask import request
    forwarded = request.headers.get("X-Forwarded-For", "")
    proxies = _trusted_proxies()
    if forwarded and proxies and (request.remote_addr or "") in proxies:
        # Only a hop we explicitly configured may supply the client address.
        hop = forwarded.split(",")[-1].strip()
        return hop[:60]
    return (request.remote_addr or "")[:60]


def bootstrap_request_allowed():
    """First-run setup is direct loopback only: (allowed, reason).

    A proxy in front of Harry would make ``remote_addr`` look local, so any
    forwarded header is refused outright rather than interpreted. Trusting one
    requires an explicit operator decision that does not exist for setup.
    """
    from flask import request
    present = [name for name in FORWARDED_HEADERS if request.headers.get(name)]
    if present:
        return False, ("first-run setup must reach Harry directly; forwarded "
                       "headers are not trusted")
    if not _is_loopback_ip(request.remote_addr):
        return False, "first-run setup is only allowed from this computer"
    if _host_name(request.host) not in LOOPBACK_HOSTS:
        return False, "first-run setup requires a loopback Host header"
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        if (parsed.scheme not in ("http", "https")
                or parsed.netloc.lower() != str(request.host or "").lower()):
            return False, "cross-origin first-run setup is refused"
    site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return False, "cross-site first-run setup is refused"
    return True, ""


def _scrub_text(value):
    """Remove secrets and local absolute paths before showing an error."""
    from . import logging_setup
    text = logging_setup.scrub(str(value))
    try:
        from . import runtime_paths
        root = runtime_paths.workspace_root()
        if root:
            text = text.replace(root, "<app>")
            text = text.replace(root.replace("\\", "/"), "<app>")
    except Exception:
        pass
    return text


def _scrub_tree(value, depth=0):
    if depth > 6:
        return "<truncated>"
    if isinstance(value, dict):
        return {str(k): _scrub_tree(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_tree(v, depth + 1) for v in value[:40]]
    if isinstance(value, str):
        return _scrub_text(value)[:2000]
    return value


def _session_token():
    from flask import session
    return session.get("admin_token") or ""


def current():
    """Validated elevation info for this request, or None."""
    from flask import session
    token = _session_token()
    if not token:
        return None
    uid = session.get("uid") or session.get("admin_uid")
    info = admin.get_admin_store().check_session(token, uid)
    if not info:
        session.pop("admin_token", None)
        return None
    return {**info, "email": session.get("admin_email", ""),
            "username": session.get("admin_uid") or uid}


def _csrf_ok():
    from flask import request, session
    if request.method in SAFE_METHODS:
        return True
    expected = session.get("admin_csrf") or ""
    supplied = request.headers.get("X-Admin-CSRF") or request.headers.get("X-CSRF-Token") or ""
    return bool(expected) and secrets.compare_digest(str(supplied), str(expected))


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        info = current()
        if not info:
            return _json({"error": "administrator mode is required"}, 403)
        if not _csrf_ok():
            return _json({"error": "invalid or missing CSRF token"}, 403)
        token = admin.set_request_admin(info)
        try:
            return view(*args, **kwargs)
        finally:
            admin.reset_request_admin(token)
    return wrapper


def _json(payload, status=200):
    from flask import jsonify
    return jsonify(payload), status


def bind_chat_context():
    """Expose the (already verified) elevation to the brain for one request."""
    info = current()
    return admin.set_request_admin(info) if info else None


def release_chat_context(token):
    if token is not None:
        admin.reset_request_admin(token)


def security_policy():
    cfg = config.CONFIG.get("admin", {}) or {}
    return {
        "role_model": "first account is administrator; other accounts are users",
        "elevation": "password re-entry + TOTP when enrolled",
        "idle_timeout_seconds": int(cfg.get("idle_seconds") or admin.IDLE_SECONDS),
        "absolute_lifetime_seconds": admin.ABSOLUTE_SECONDS,
        "automatic_repairs": ["documents_index", "media_index", "jobs", "tts", "stt"],
        "approval_required": ["change_role", "security_policy", "activate_upgrade",
                             "restore_backup", "install_plugin", "deploy"],
        "self_modification_allowed": ["plugins (validated + approved)",
                                      "non-sensitive prompts",
                                      "documentation", "tests"],
        "forbidden": ["editing security modules automatically",
                      "changing permissions without an approval",
                      "deleting audit records", "committing secrets"],
        "hosted": str(os.environ.get("HARRY_HOSTED", "")).lower() in ("1", "true", "yes"),
    }


def _tile_summary():
    snapshot = monitoring.status().get("snapshot") or {}
    checks = snapshot.get("checks") or {}
    voice_checks = {k: v for k, v in checks.items() if k in ("stt", "tts")}
    return {
        "backend": "healthy" if snapshot.get("ok") else ("degraded" if snapshot else "unknown"),
        "backendOk": bool(snapshot.get("ok")),
        "model": ", ".join(checks.get("ollama", {}).get("models", [])[:2]) or "unavailable",
        "modelOk": bool(checks.get("ollama", {}).get("ok")),
        "voice": "ready" if all(v.get("ok") for v in voice_checks.values()) and voice_checks
                 else "check needed",
        "voiceOk": bool(voice_checks) and all(v.get("ok") for v in voice_checks.values()),
        "diskFree": f"{checks.get('disk', {}).get('free_gb', '?')} GB",
        "diskOk": (checks.get("disk", {}).get("value") or 0) < monitoring.settings()["disk_warn_percent"],
        "sessions": len([s for s in admin.get_admin_store().sessions() if s["active"]]),
        "unhealthy": str(len(snapshot.get("failed") or [])),
    }


# ------------------------------------------------------------------ install

def install(app):
    from flask import jsonify, redirect, request, session

    if not app.secret_key:
        # Local desktop mode has no hosted module setting this; a per-boot key
        # keeps the elevation cookie signed without persisting a secret.
        app.secret_key = os.environ.get("HARRY_LOCAL_SECRET") or secrets.token_hex(32)

    def _hosted():
        from . import hosted
        return hosted.enabled()

    @app.get("/admin")
    def admin_page():
        if _hosted() and not session.get("uid"):
            return redirect("/login")
        response = app.response_class(PAGE, mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/admin/session")
    def admin_session():
        info = current()
        identity = session.get("uid") or session.get("admin_uid")
        role = ""
        if identity:
            role = admin.get_admin_store().role(identity)
        if info and not session.get("admin_csrf"):
            session["admin_csrf"] = secrets.token_urlsafe(24)
        needs_setup = False
        try:
            from .accounts import get_store
            needs_setup = get_store().count() == 0
        except Exception:
            needs_setup = False
        return jsonify({
            "authenticated": bool(identity),
            "admin": bool(info),
            "needs_setup": needs_setup,
            "email": (info or {}).get("email") or session.get("harry_email", ""),
            "role": role,
            "csrf": session.get("csrf") or session.get("admin_csrf") or "",
            "mfa": bool(info and admin.get_admin_store().totp_required(info["user_id"])),
        })

    @app.post("/api/admin/enter")
    def admin_enter():
        # CSRF for the sign-in step comes from the hosted gate or the admin token.
        if _hosted():
            expected = session.get("csrf") or ""
        else:
            expected = session.get("csrf") or ""
        supplied = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("csrf", "")
        if expected and not secrets.compare_digest(str(supplied), str(expected)) and supplied:
            return jsonify({"error": "invalid CSRF token"}), 403
        if _hosted():
            uid = session.get("uid")
            if not uid:
                return jsonify({"error": "sign in first"}), 401
            user = admin.get_admin_store()
            if not user.is_admin(uid):
                return jsonify({"error": "this account is not an administrator"}), 403
            target = uid
        else:
            data = request.get_json(silent=True) or {}
            email = str(data.get("email") or "").strip().lower()
            store = admin.get_admin_store()
            ids = store.admin_ids()
            if email:
                from .accounts import get_store
                found = get_store().by_email(email)
                target = found["id"] if found else ""
                if target and not store.is_admin(target):
                    return jsonify({"error": "this account is not an administrator"}), 403
            elif len(ids) == 1:
                target = ids[0]["id"]
            elif not ids:
                return jsonify({"error": "create an administrator account first: "
                                         "python -m harry_core.hosted --create-user you@example.com"}), 409
            else:
                return jsonify({"error": "several administrators exist; provide your email",
                                "email_required": True}), 400
            if not target:
                return jsonify({"error": "email or password is incorrect"}), 401
        data = request.get_json(silent=True) or {}
        result = admin.get_admin_store().elevate(
            target, data.get("password", ""), data.get("code", ""),
            ip=_client_ip(), user_agent=request.headers.get("User-Agent", ""))
        if not result.get("ok"):
            status = 401
            if result.get("mfa"):
                status = 401
            return jsonify({"error": result.get("error", "authentication failed"),
                            "mfa": bool(result.get("mfa"))}), status
        from .accounts import get_store
        account = get_store().get(target) or {}
        session["admin_token"] = result["token"]
        session["admin_uid"] = target
        session["admin_email"] = account.get("email", "")
        # One token for both layers: the hosted gate compares X-CSRF-Token with
        # session["csrf"], and admin_required compares X-Admin-CSRF with this.
        # Reusing one value means a browser cannot satisfy one check and fail the
        # other by accident.
        existing = session.get("csrf") or ""
        session["admin_csrf"] = existing or secrets.token_urlsafe(24)
        if not _hosted():
            session["csrf"] = session["admin_csrf"]
        return jsonify({"ok": True, "csrf": session["admin_csrf"],
                        "expires_in": result["expires_in"],
                        "idle_seconds": result["idle_seconds"]})

    @app.post("/api/admin/bootstrap")
    def admin_bootstrap():
        """Create the very first administrator. Direct loopback only."""
        from .accounts import get_store
        if _hosted():
            return jsonify({"error": "create accounts from the operator CLI on hosted Harry"}), 403
        allowed, reason = bootstrap_request_allowed()
        if not allowed:
            admin.get_admin_store().audit(None, "bootstrap_refused",
                                          {"reason": reason}, _client_ip())
            return jsonify({"error": reason}), 403
        body = request.get_json(silent=True) or {}
        created = get_store().create(body.get("email", ""), body.get("password", ""))
        if not created.get("ok"):
            error = created.get("error", "could not create the account")
            return jsonify({"error": error}), (409 if "already registered" in error else 400)
        if created.get("role") != admin.ROLE_ADMIN:
            # A concurrent request created the administrator first; this account
            # must not exist as a half-finished leftover.
            get_store().delete(created["id"])
            return jsonify({"error": "an account already exists"}), 409
        admin.get_admin_store().audit(created.get("id"), "admin_bootstrapped",
                                      {"email": created.get("email")}, _client_ip())
        return jsonify({"ok": True, "email": created.get("email"),
                        "role": created.get("role")})

    @app.post("/api/admin/exit")
    def admin_exit():
        token = session.pop("admin_token", None)
        if token:
            admin.get_admin_store().revoke(token, actor=session.get("admin_uid"))
        session.pop("admin_csrf", None)
        return jsonify({"ok": True})

    # -- health ------------------------------------------------------------

    @app.get("/api/admin/health")
    @admin_required
    def admin_health():
        monitoring.ensure_started()
        state = monitoring.status()
        return jsonify({
            "tiles": _tile_summary(),
            "checks": (state.get("snapshot") or {}).get("checks", {}),
            "alerts": selfheal.alerts()[-20:],
            "monitor": {"running": state["running"], "interval": state["interval"]},
            "unhealthy": [name for name, item in selfheal.diagnose().items()
                          if not item.get("ok")],
        })

    @app.post("/api/admin/diagnostics")
    @admin_required
    def admin_diagnostics():
        from .diagnostics import run_diagnostics
        report = run_diagnostics()
        component = (request.get_json(silent=True) or {}).get("component")
        if component:
            return jsonify({"report": report, "component": selfheal.diagnose(component)})
        return jsonify({"report": report})

    @app.post("/api/admin/heal")
    @admin_required
    def admin_heal():
        info = admin.request_admin()
        component = (request.get_json(silent=True) or {}).get("component") or "all"
        if component == "all":
            results = {name: selfheal.heal(name, actor=info["user_id"])
                       for name, item in selfheal.diagnose().items()
                       if not item.get("ok")}
            return jsonify({"ok": True, "results": results})
        return jsonify(selfheal.heal(component, actor=info["user_id"]))

    # -- capabilities / plugins -------------------------------------------

    @app.get("/api/admin/capabilities")
    @admin_required
    def admin_capabilities():
        capabilities.sync_tools()
        items = capabilities.list_capabilities()
        return jsonify({
            "items": items,
            "plugins": len([i for i in items if i.get("source") == "plugin"]),
            "failed": len([i for i in items if i.get("status") == "failed"]),
        })

    @app.post("/api/admin/capabilities/<name>/test")
    @admin_required
    def admin_capability_test(name):
        body = request.get_json(silent=True) or {}
        result = capabilities.test_capability(name, body.get("args") or {})
        return jsonify(result)

    @app.get("/api/admin/plugins")
    @admin_required
    def admin_plugins():
        records = {c["name"]: c for c in capabilities.list_capabilities()}
        items = []
        for entry in capabilities.discover():
            name = (entry.get("manifest") or {}).get("name") or entry["id"]
            record = records.get(name) or {}
            items.append({
                "id": entry["id"], "name": name,
                "version": (entry.get("manifest") or {}).get("version", ""),
                "valid": entry["valid"], "problems": entry["problems"],
                "status": record.get("status") or ("valid" if entry["valid"] else "invalid"),
            })
        return jsonify({"items": items})

    @app.post("/api/admin/plugins/<plugin_id>/approve")
    @admin_required
    def admin_plugin_approve(plugin_id):
        info = admin.request_admin()
        entries = {e["id"]: e for e in capabilities.discover()}
        entry = entries.get(plugin_id)
        if not entry:
            return jsonify({"error": "unknown plugin"}), 404
        if not entry["valid"]:
            return jsonify({"error": "the plugin failed validation", "problems": entry["problems"]}), 400
        result = capabilities.install_pending(approved_ids={plugin_id})
        admin.get_admin_store().audit(info["user_id"], "plugin_approved",
                                      {"plugin": plugin_id})
        return jsonify({"ok": True, "results": result})

    # -- upgrades ----------------------------------------------------------

    @app.get("/api/admin/upgrades")
    @admin_required
    def admin_upgrades():
        return jsonify({"items": selfdev.list_upgrades()})

    @app.post("/api/admin/upgrades/propose")
    @admin_required
    def admin_upgrade_propose():
        info = admin.request_admin()
        body = request.get_json(silent=True) or {}
        request_text = str(body.get("request") or "").strip()
        if not request_text:
            return jsonify({"error": "describe what the capability should do"}), 400
        name = body.get("name") or None
        code = body.get("code")
        description = body.get("description")
        if not code:
            generated = selfdev.generate_plugin_code(request_text, name=name)
            if not generated.get("ok"):
                return jsonify({"error": generated.get("error", "generation failed")}), 422
            code = generated["code"]
            description = description or generated.get("description")
            name = name or generated.get("name")
        record = selfdev.plan_plugin(request_text, name=name, code=code,
                                     description=description,
                                     permissions=body.get("permissions"))
        admin.get_admin_store().audit(info["user_id"], "upgrade_proposed",
                                      {"upgrade": record["id"], "name": record["name"],
                                       "status": record["status"]})
        if record["status"] == "invalid":
            return jsonify({"id": record["id"], "status": "invalid",
                            "problems": record["problems"]}), 422
        approval = selfdev.request_approval(record["id"], requested_by=info["user_id"])
        return jsonify({"id": record["id"], "name": record["name"],
                        "status": record["status"], "needs_approval": True,
                        "approval": approval.get("id"),
                        "problems": record["problems"]})

    @app.post("/api/admin/upgrades/<upgrade_id>/request-approval")
    @admin_required
    def admin_upgrade_request(upgrade_id):
        info = admin.request_admin()
        return jsonify(selfdev.request_approval(upgrade_id, requested_by=info["user_id"]))

    @app.post("/api/admin/upgrades/<upgrade_id>/activate")
    @admin_required
    def admin_upgrade_activate(upgrade_id):
        info = admin.request_admin()
        return jsonify(selfdev.activate(upgrade_id, actor=info["user_id"]))

    @app.post("/api/admin/upgrades/<upgrade_id>/rollback")
    @admin_required
    def admin_upgrade_rollback(upgrade_id):
        info = admin.request_admin()
        return jsonify(selfdev.rollback(upgrade_id, actor=info["user_id"]))

    @app.get("/api/admin/backups")
    @admin_required
    def admin_backups():
        return jsonify({"items": selfdev.backups()})

    # -- approvals ---------------------------------------------------------

    @app.get("/api/admin/approvals")
    @admin_required
    def admin_approvals():
        return jsonify({"items": admin.get_admin_store().approvals()})

    @app.post("/api/admin/approvals/<approval_id>")
    @admin_required
    def admin_approval_decide(approval_id):
        info = admin.request_admin()
        body = request.get_json(silent=True) or {}
        result = admin.get_admin_store().decide_approval(
            approval_id, bool(body.get("approve")), decided_by=info["user_id"],
            note=body.get("note", ""))
        if not result.get("ok"):
            return jsonify({"error": result.get("error")}), 400
        return jsonify(result)

    @app.get("/api/admin/audit")
    @admin_required
    def admin_audit():
        return jsonify({"items": admin.get_admin_store().audit_entries(200)})

    @app.get("/api/admin/sessions")
    @admin_required
    def admin_sessions():
        return jsonify({"items": admin.get_admin_store().sessions()})

    @app.post("/api/admin/sessions/<session_id>/revoke")
    @admin_required
    def admin_session_revoke(session_id):
        info = admin.request_admin()
        result = admin.get_admin_store().revoke_by_id(session_id, actor=info["user_id"])
        return jsonify(result), (200 if result.get("ok") else 404)

    @app.get("/api/admin/policy")
    @admin_required
    def admin_policy():
        return jsonify(security_policy())

    @app.post("/api/admin/users/<user_id>/role")
    @admin_required
    def admin_user_role(user_id):
        """Role changes need a separate approved record: never implicit."""
        info = admin.request_admin()
        body = request.get_json(silent=True) or {}
        role = str(body.get("role") or "")
        if role not in (admin.ROLE_ADMIN, admin.ROLE_USER):
            return jsonify({"error": "role must be 'admin' or 'user'"}), 400
        if not admin.get_admin_store().role(user_id):
            return jsonify({"error": "unknown account"}), 404
        approval_id = str(body.get("approval_id") or "")
        store = admin.get_admin_store()
        match = {"user_id": user_id, "role": role}
        record = None
        if approval_id and store.approved(approval_id, "change_role"):
            record = {"id": approval_id}
        else:
            record = store.find_approved("change_role", match)
        if not record:
            pending = store.request_approval(
                "change_role", match, risk=admin.HIGH,
                requested_by=info["user_id"],
                note="role change needs a recorded approval")
            return jsonify({"error": "this change needs an approval record",
                            "approval": pending.get("id"), "status": "pending"}), 202
        result = store.set_role(user_id, role, actor=info["user_id"])
        if not result.get("ok"):
            return jsonify({"error": result.get("error")}), 400
        store.consume_approval(record["id"], actor=info["user_id"])
        store.audit(info["user_id"], "role_changed",
                    {"user": user_id, "role": role, "approval": record["id"]})
        return jsonify(result)

    def _user_row(row, admin_store):
        return {"id": row["id"], "email": row["email"], "name": row["name"],
                "created": row["created"], "active": bool(row["active"]),
                "role": row["role"] or "user",
                "administrator": admin_store.is_admin(row["id"])}

    @app.get("/api/admin/users")
    @admin_required
    def admin_users():
        from .accounts import get_store
        store = get_store()
        admin_store = admin.get_admin_store()
        with store._connect() as db:
            rows = db.execute("SELECT id, email, name, created, active, role "
                              "FROM users ORDER BY created").fetchall()
        return jsonify({"items": [_user_row(row, admin_store) for row in rows]})

    @app.post("/api/admin/users")
    @admin_required
    def admin_user_create():
        info = admin.request_admin()
        from .accounts import get_store
        body = request.get_json(silent=True) or {}
        email = str(body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        name = body.get("name") or ""
        if not email or not password:
            return jsonify({"error": "email and password are required"}), 400
        if str(body.get("role") or "") not in ("", admin.ROLE_USER):
            # New accounts start as users; becoming an administrator needs the
            # separate, approved role change.
            return jsonify({"error": "new accounts are created as users; use the "
                                     "role endpoint to promote them"}), 400
        created = get_store().create(email, password, name)
        if not created.get("ok"):
            error = created.get("error", "could not create the account")
            return jsonify({"error": error}), (409 if "already registered" in error else 400)
        admin.get_admin_store().audit(info["user_id"], "user_created",
                                      {"user": created.get("id"),
                                       "email": created.get("email")})
        return jsonify({"ok": True, "id": created.get("id"),
                        "email": created.get("email"),
                        "role": created.get("role")}), 201

    @app.route("/api/admin/users/<user_id>", methods=["PATCH", "DELETE"])
    @admin_required
    def admin_user_modify(user_id):
        info = admin.request_admin()
        from .accounts import get_store
        store = get_store()
        admin_store = admin.get_admin_store()
        account = store.get(user_id)
        if not account:
            return jsonify({"error": "unknown account"}), 404
        if request.method == "DELETE":
            if user_id == info["user_id"]:
                return jsonify({"error": "you cannot delete the account you are signed in as"}), 409
            if admin_store.is_admin(user_id) and len(admin_store.admin_ids()) <= 1:
                return jsonify({"error": "the last administrator cannot be deleted"}), 409
            approval_id = str((request.get_json(silent=True) or {}).get("approval_id") or "")
            record = None
            if approval_id and admin_store.approved(approval_id, "delete_user"):
                record = {"id": approval_id}
            else:
                record = admin_store.find_approved("delete_user", {"user_id": user_id})
            if not record:
                pending = admin_store.request_approval(
                    "delete_user", {"user_id": user_id}, risk=admin.HIGH,
                    requested_by=info["user_id"],
                    note="deleting an account needs a recorded approval")
                return jsonify({"error": "deleting an account needs an approval record",
                                "approval": pending.get("id"), "status": "pending"}), 202
            revoked = admin_store.revoke_user_sessions(
                user_id, actor=info["user_id"], reason="account deleted")
            store.delete(user_id)
            admin_store.consume_approval(record["id"], actor=info["user_id"])
            admin_store.audit(info["user_id"], "user_deleted",
                              {"user": user_id, "email": account.get("email"),
                               "sessions_revoked": revoked,
                               "approval": record["id"]})
            return jsonify({"ok": True, "deleted": user_id})

        body = request.get_json(silent=True) or {}
        allowed = {"name", "active", "approval_id"}
        unknown = set(body) - allowed
        if unknown:
            return jsonify({"error": f"unknown field(s): {', '.join(sorted(unknown))}"}), 400
        if not ({"name", "active"} & set(body)):
            return jsonify({"error": "provide name or active"}), 400
        if "name" in body:
            if not isinstance(body["name"], str) or len(body["name"].strip()) > 80:
                return jsonify({"error": "name must be text of at most 80 characters"}), 400
            store.rename(user_id, body["name"])
        if "active" in body:
            if not isinstance(body["active"], bool):
                return jsonify({"error": "active must be true or false"}), 400
            if not body["active"]:
                if user_id == info["user_id"]:
                    return jsonify({"error": "you cannot deactivate yourself while "
                                             "using admin mode"}), 409
                if admin_store.is_admin(user_id) and len(admin_store.admin_ids()) <= 1:
                    return jsonify({"error": "the last administrator cannot be "
                                             "deactivated"}), 409
                revoked = admin_store.revoke_user_sessions(
                    user_id, actor=info["user_id"], reason="account deactivated")
                store.set_active(user_id, False)
                admin_store.audit(info["user_id"], "user_deactivated",
                                  {"user": user_id, "sessions_revoked": revoked})
                return jsonify({"ok": True, "active": False,
                                "sessions_revoked": revoked})
            store.set_active(user_id, True)
            admin_store.audit(info["user_id"], "user_activated", {"user": user_id})
        updated = store.get(user_id) or {}
        return jsonify({"ok": True, "id": user_id,
                        "name": updated.get("name") or "",
                        "active": bool(updated.get("active", 1)),
                        "role": updated.get("role") or "user"})

    @app.get("/api/admin/errors")
    @admin_required
    def admin_errors():
        info = admin.request_admin()
        from . import jobs

        def _number(name, default, low, high):
            raw = request.args.get(name)
            if raw is None or str(raw).strip() == "":
                return default, None
            try:
                value = int(str(raw).strip())
            except ValueError:
                return None, f"{name} must be an integer"
            if value < low or value > high:
                return None, f"{name} must be between {low} and {high}"
            return value, None

        limit, problem = _number("limit", 25, 1, 200)
        if problem:
            return jsonify({"error": problem}), 400
        offset, problem = _number("offset", 0, 0, 100000)
        if problem:
            return jsonify({"error": problem}), 400
        kind = str(request.args.get("kind") or "").strip().lower()
        if kind and kind not in ("error", "repair"):
            return jsonify({"error": "kind must be 'error' or 'repair'"}), 400
        component = str(request.args.get("component") or "").strip()
        if component and component not in selfheal.components():
            return jsonify({"error": "unknown component"}), 400

        history = [h for h in selfheal.recent(200)
                   if h.get("kind") in ("error", "repair")]
        if kind:
            history = [h for h in history if h.get("kind") == kind]
        if component:
            history = [h for h in history if h.get("component") == component]
        total = len(history)
        page = history[offset:offset + limit]
        failed = [j for j in jobs.get_jobs().list(limit=30)
                  if j.get("state") == "error"]
        admin.get_admin_store().audit(info["user_id"], "errors_viewed",
                                      {"count": len(page), "total": total,
                                       "offset": offset})
        return jsonify({
            "history": [_scrub_tree(h) for h in page],
            "failed_jobs": [_scrub_tree(j) for j in failed],
            "total": total, "limit": limit, "offset": offset,
            "next_offset": (offset + limit) if offset + limit < total else None,
        })

    @app.get("/api/admin/errors/<error_id>")
    @admin_required
    def admin_error_detail(error_id):
        info = admin.request_admin()
        if not re.match(r"^[0-9a-f]{6,32}$", str(error_id or "")):
            return jsonify({"error": "invalid error id"}), 404
        item = selfheal.entry(error_id)
        if not item:
            return jsonify({"error": "unknown error"}), 404
        admin.get_admin_store().audit(info["user_id"], "error_viewed",
                                      {"id": error_id})
        return jsonify({"item": _scrub_tree(item)})

    # -- MFA ---------------------------------------------------------------

    @app.post("/api/admin/mfa/enroll")
    @admin_required
    def admin_mfa_enroll():
        info = admin.request_admin()
        return jsonify(admin.get_admin_store().enroll_totp(info["user_id"]))

    @app.post("/api/admin/mfa/confirm")
    @admin_required
    def admin_mfa_confirm():
        info = admin.request_admin()
        code = (request.get_json(silent=True) or {}).get("code", "")
        result = admin.get_admin_store().confirm_totp(info["user_id"], code)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/admin/mfa/disable")
    @admin_required
    def admin_mfa_disable():
        info = admin.request_admin()
        admin.get_admin_store().disable_totp(info["user_id"])
        return jsonify({"ok": True})

    @app.post("/api/admin/alerts/seen")
    @admin_required
    def admin_alerts_seen():
        return jsonify(monitoring.mark_alerts_seen())

    # Start the monitor only when the panel is opened; a desktop user who never
    # touches admin mode pays nothing for it.
    return app
