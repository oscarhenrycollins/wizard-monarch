'use strict';
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let st = null, started = false, poll = null;

function say(text, kind = '') { const s = el('status'); s.classList.toggle('wz-sr', !kind); s.classList.toggle('note', kind === 'note'); s.textContent = text; }
async function api(path, body) {
  const init = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  let out = {};
  try { out = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(out.error || 'Something went wrong. Try again.');
  return out;
}
function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const money = (n) => n == null ? '' : new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(n);
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'never';
const KIND = { depository: 'Cash', credit: 'Credit cards', investment: 'Investments', loan: 'Loans', property: 'Property', vehicle: 'Vehicles', crypto: 'Crypto', other: 'Other', liability: 'Other debts' };

function render(s) {
  st = s;
  const linked = s.status !== 'not_connected' && s.status !== 'needs_login';
  const hasData = s.banks.length > 0;
  el('steps').hidden = linked;
  el('lead').hidden = linked;
  el('conn').hidden = !linked && !hasData;
  el('s-open').classList.toggle('is-now', !started);
  el('s-open').classList.toggle('is-done', started);
  el('s-paste').classList.toggle('is-now', started);
  el('s-paste').classList.toggle('is-later', !started);
  el('disconnect').hidden = !linked && !hasData;
  el('v-scope').textContent = s.scope ? (s.scope.includes('write') ? 'Read and write' : 'Read-only') : 'Not connected';
  el('v-calls').textContent = `${s.calls_this_week || 0} this week`;
  el('months').value = String(s.history_months);

  const bits = [s.syncing ? 'Syncing now…' : `Synced ${when(s.last_sync)}`, `${s.transactions.toLocaleString()} transactions`];
  if (s.transactions_from) bits.push('since ' + new Date(s.transactions_from + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', year: 'numeric' }));
  el('sync-note').textContent = bits.join(' · ');
  el('sync').disabled = s.syncing;

  const warn = s.status === 'needs_login' ? (s.last_error || 'Monarch ended this sign-in.') : s.status === 'rate_limited' ? s.last_error : (!s.syncing && s.last_error) || '';
  el('warn').hidden = !warn; el('warn-t').textContent = warn;
  el('relogin').hidden = s.status !== 'needs_login';

  // Net worth strip from the account copy
  const accts = s.banks.flatMap((b) => b.accounts).filter((a) => a.status === 'open' && !a.hidden);
  const assets = accts.filter((a) => a.is_asset !== false).reduce((t, a) => t + (a.ledger || 0), 0);
  const debts = accts.filter((a) => a.is_asset === false).reduce((t, a) => t + Math.abs(a.ledger || 0), 0);
  const k = el('kpis'); k.replaceChildren();
  if (accts.length) for (const [v, l] of [[assets - debts, 'net worth'], [assets, 'assets'], [debts, 'debts']]) { const d = h('div', 'mn-kpi'); d.append(h('b', '', money(Math.round(v))), h('span', '', l)); k.append(d); }

  // Accounts grouped by kind, like Monarch's own Accounts page
  const box = el('insts'); box.replaceChildren();
  const groups = {};
  for (const a of accts) (groups[a.type] ||= []).push(a);
  for (const kind of Object.keys(KIND)) {
    const list = groups[kind]; if (!list) continue;
    const sec = h('section', 'mn-group');
    const tot = list.reduce((t, a) => t + (a.ledger || 0), 0);
    const hd = h('div', 'mn-group-h'); hd.append(h('b', '', KIND[kind]), h('span', '', money(tot)));
    sec.append(hd);
    const ul = h('ul', 'mn-accts');
    for (const a of list.sort((x, y) => Math.abs(y.ledger || 0) - Math.abs(x.ledger || 0))) {
      const li = h('li');
      const n = h('span', 'mn-acct-n'); n.append(h('b', '', a.name || 'Account'), h('small', '', [a.institution, a.last_four && '··' + a.last_four].filter(Boolean).join(' · ')));
      li.append(n, h('span', 'mn-bal', money(a.ledger)));
      ul.append(li);
    }
    sec.append(ul); box.append(sec);
  }
  if (linked && !accts.length) box.append(h('p', 'mn-small mn-empty', s.syncing ? 'Pulling your accounts and history from Monarch. This takes a minute or two the first time.' : 'No accounts yet. Tap Sync now.'));

  clearTimeout(poll);
  if (s.syncing) poll = setTimeout(load, 4000);
}

const load = () => api('api/status').then(render).catch((e) => say(e.message, 'error'));

async function openLogin() {
  // Open the tab first (inside the tap) so phones don't block it, then point it at Monarch.
  const tab = window.open('about:blank', '_blank');
  try {
    const r = await api('api/login/start', {});
    if (tab) tab.location.href = r.url; else location.href = r.url;
    started = true; render(st); el('paste').focus({ preventScroll: true });
  } catch (e) { if (tab) tab.close(); say(e.message, 'error'); }
}
el('open').onclick = openLogin;
el('relogin').onclick = () => { started = false; el('steps').hidden = false; render({ ...st, status: 'not_connected' }); openLogin(); };
el('again').onclick = () => { started = false; el('paste').value = ''; render(st); };
el('paste-form').onsubmit = async (e) => {
  e.preventDefault();
  el('paste-go').disabled = true;
  try { render(await api('api/login/finish', { url: el('paste').value.trim() })); el('paste').value = ''; started = false; say('Monarch connected. Pulling your accounts…', 'note'); }
  catch (err) { say(err.message, 'error'); }
  finally { el('paste-go').disabled = false; }
};
el('sync').onclick = async () => {
  el('sync').disabled = true; say('Syncing…', 'note');
  try { render(await api('api/sync', {})); say('Up to date.', 'note'); }
  catch (e) { say(e.message, 'error'); load(); }
};
el('months').onchange = (e) => api('api/settings', { history_months: Number(e.target.value) }).then((s) => { render(s); say('Saved. The next sync fills in the history.', 'note'); }).catch((err) => say(err.message, 'error'));
el('disconnect').onclick = async () => {
  if (!confirm('Disconnect Monarch? This app signs out of Monarch and deletes its copy of your data. Your Monarch account is not touched.')) return;
  try { render(await api('api/disconnect', {})); say('Disconnected.', 'note'); } catch (e) { say(e.message, 'error'); }
};

window.__wizardCloseOverlay = () => {
  if (el('settings').open) { el('settings').open = false; return true; }
  if (started) { started = false; render(st); return true; }
  return false;
};
if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { window.__wizardCloseOverlay(); history.pushState({ wzGuard: 1 }, '', location.href); });
}
load();
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
