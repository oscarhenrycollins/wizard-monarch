'use strict';
// The connector's own screen: connect guide, connection status and Disconnect. Nothing financial.
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let st = null, started = false, guide = false, poll = null;

function say(text, kind = '') { const s = el('status'); s.classList.toggle('note', kind === 'note'); s.textContent = text; }
async function api(path, body) {
  const init = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  let out = {};
  try { out = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(out.error || 'Something went wrong. Try again.');
  return out;
}
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'Not yet';

const STATE = { connected: 'Connected', rate_limited: 'Connected · weekly limit reached', needs_login: 'Needs sign-in', not_connected: 'Not connected' };


function render(s) {
  st = s;
  const linked = s.status === 'connected' || s.status === 'rate_limited';
  const showGuide = !linked || guide;
  el('guide').hidden = !showGuide;
  el('conn').hidden = !linked && s.status !== 'needs_login';
  el('state-t').textContent = STATE[s.status] || s.status;
  el('dot').dataset.state = s.status;
  el('v-access').textContent = s.access ? 'Read only' : '—';
  el('v-sync').textContent = s.syncing ? 'Syncing now…' : when(s.last_sync);
  const warn = s.status === 'needs_login' ? (s.last_error || 'Monarch ended this sign-in.') :
    s.status === 'rate_limited' ? (s.last_error || "Monarch's weekly limit is used up. Syncing resumes on its own.") :
    (!s.syncing && s.last_error) || '';
  el('warn').hidden = !warn; el('warn').textContent = warn;
  el('relogin').hidden = s.status !== 'needs_login' || showGuide;

  el('disconnect').hidden = !linked && s.status !== 'needs_login';
  el('s-access').className = 'mn-step ' + (started ? 'is-done' : 'is-now');

  el('s-paste').className = 'mn-step ' + (started ? 'is-now' : 'is-later');

  clearTimeout(poll);
  if (s.syncing) poll = setTimeout(load, 5000);
}

const load = () => api('api/connection').then(render).catch((e) => say(e.message));

async function openLogin() {
  // Open the tab inside the tap so phones don't block it, then point it at Monarch.
  const tab = window.open('about:blank', '_blank');
  try {
    const r = await api('api/login/start', {});
    if (tab) tab.location.href = r.url; else location.href = r.url;
    started = true; render(st); el('paste').focus({ preventScroll: true });
  } catch (e) { if (tab) tab.close(); say(e.message); }
}
function showGuide() {
  guide = true; started = false;
  render(st);
}
el('open').onclick = openLogin;
el('relogin').onclick = showGuide;
el('again').onclick = () => { started = false; el('paste').value = ''; render(st); };
el('paste-form').onsubmit = async (e) => {
  e.preventDefault();
  el('paste-go').disabled = true;
  try {
    await api('api/login/finish', { url: el('paste').value.trim() });
    el('paste').value = ''; started = false; guide = false;
    await load();
    say('Connected. Read only.', 'note');
  } catch (err) { say(err.message); }
  finally { el('paste-go').disabled = false; }
};
el('disconnect').onclick = async () => {
  if (!confirm('Disconnect Monarch? This app signs out of Monarch and deletes its copy of your data. Your Monarch account is not touched.')) return;
  try { await api('api/disconnect', {}); guide = false; started = false; await load(); say('Disconnected.', 'note'); } catch (e) { say(e.message); }
};

window.__wizardCloseOverlay = () => {
  if (started) { started = false; render(st); return true; }
  if (guide && st && (st.status === 'connected' || st.status === 'rate_limited')) { guide = false; render(st); return true; }
  return false;
};
if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { window.__wizardCloseOverlay(); history.pushState({ wzGuard: 1 }, '', location.href); });
}
load();
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
