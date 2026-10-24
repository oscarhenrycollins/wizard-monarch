"""Monarch: a finance connector for a Wizard box.

The owner signs in to Monarch once (OAuth 2.1 + PKCE against Monarch's official MCP server, read-only scope).
Monarch only accepts loopback return addresses, so after tapping Allow the owner pastes the return address back
into this app's screen; the one-time code is useless without the PKCE verifier that never leaves this app.

The app keeps the tokens private, syncs accounts and transactions into a local copy on a schedule (Monarch's
MCP has a weekly usage limit, so screens and other apps read the copy, not Monarch), and offers them read-only
to other apps on this box through MCP with the same contract as the Teller connector
(wizard.bank-connection/1), plus Monarch extras (budget, cash flow, net worth, recurring) and a live read-only
pass-through to Monarch's own tools.

Stdlib only. Secrets live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-monarch'
VERSION = '0.1.0'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8806')
BASE = os.environ.get('MONARCH_BASE', 'https://api.monarch.com').rstrip('/')
MCP_URL = BASE + '/mcp'
RESOURCE = os.environ.get('MONARCH_RESOURCE', 'https://api.monarch.com/mcp')
REDIRECT = os.environ.get('MONARCH_REDIRECT', 'http://127.0.0.1:8806/oauth/callback')
SCOPE = 'mcp:read'
SYNC_HOURS = float(os.environ.get('MONARCH_SYNC_HOURS', '12'))
SNAPSHOT_HOURS = float(os.environ.get('MONARCH_SNAPSHOT_HOURS', '24'))
RECENT_DAYS = int(os.environ.get('MONARCH_RECENT_DAYS', '45'))
PAGE = int(os.environ.get('MONARCH_PAGE', '1000'))
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
PROTOCOL = '2025-06-18'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SNAPSHOTS = {'budget': 'GetBudget', 'cashflow': 'GetCashFlow', 'net_worth': 'GetNetWorthHistory',
             'recurring': 'GetRecurring', 'categories': 'GetCategories', 'investments': 'GetInvestments'}
RATE_MSG = "Monarch's weekly MCP limit is used up. It resets weekly; syncing resumes on its own."
LOCK = threading.RLock()        # local files + database
REMOTE = threading.RLock()      # one conversation with Monarch at a time
SYNC_LOCK = threading.Lock()


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class MonarchError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _config():
    return _read('config.json')


def _save_config(c):
    with LOCK:
        _write_secret('config.json', json.dumps(c))


def _update_config(**kv):
    with LOCK:
        c = _config()
        for k, v in kv.items():
            if v is None:
                c.pop(k, None)
            else:
                c[k] = v
        _save_config(c)
        return c


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            (DATA / 'tokens.json').unlink(missing_ok=True)


def db():
    c = sqlite3.connect(_private() / 'monarch.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, name TEXT, institution TEXT, type TEXT, kind TEXT,
        subtype TEXT, mask TEXT, is_asset INTEGER, balance REAL, signed_balance REAL, credit_limit REAL, closed INTEGER,
        hidden INTEGER, raw TEXT, seen TEXT);
      CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY, account TEXT, date TEXT, amount REAL, merchant TEXT,
        description TEXT, category TEXT, category_group TEXT, category_type TEXT, pending INTEGER, raw TEXT, seen TEXT);
      CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
      CREATE INDEX IF NOT EXISTS tx_acct ON transactions(account, date);
      CREATE TABLE IF NOT EXISTS snapshots (kind TEXT PRIMARY KEY, at TEXT, data TEXT, error TEXT);
    ''')
    os.chmod(DATA / 'monarch.sqlite3', 0o600)
    return c


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _ago(iso):
    if not iso:
        return float('inf')
    return time.time() - dt.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()


def _week():
    y, w, _ = dt.date.today().isocalendar()
    return f'{y}-W{w:02d}'


# ---------------- HTTP to Monarch ----------------

def _http(method, url, data=None, form=None, headers=None, timeout=60):
    h = {'User-Agent': UA, 'Accept': 'application/json', **(headers or {})}
    body = None
    if form is not None:
        body = urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif data is not None:
        body = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except OSError as e:
        raise MonarchError(0, 'network', f'Could not reach Monarch: {e}') from None


def _json(b):
    try:
        return json.loads(b or b'{}')
    except ValueError:
        return {}


def _meta():
    cfg = _config()
    m = cfg.get('oauth_meta')
    if m and cfg.get('oauth_meta_base') == BASE and _ago(cfg.get('oauth_meta_at')) < 86400:
        return m
    st, _, b = _http('GET', BASE + '/.well-known/oauth-authorization-server')
    m = _json(b)
    if st != 200 or not m.get('authorization_endpoint') or not m.get('token_endpoint'):
        raise MonarchError(st, 'discovery', "Monarch's sign-in service isn't answering right now. Try again in a few minutes.")
    _update_config(oauth_meta=m, oauth_meta_base=BASE, oauth_meta_at=_now())
    return m


# ---------------- sign-in (OAuth 2.1 + PKCE, public client) ----------------

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _client_id(meta):
    cfg = _config()
    if cfg.get('client_id') and cfg.get('client_redirect') == REDIRECT and cfg.get('client_base') == BASE:
        return cfg['client_id']
    st, _, b = _http('POST', meta['registration_endpoint'], data={
        'client_name': 'Wizard Monarch', 'redirect_uris': [REDIRECT], 'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'token_endpoint_auth_method': 'none', 'scope': SCOPE})
    d = _json(b)
    if st not in (200, 201) or not d.get('client_id'):
        raise MonarchError(st, 'register', d.get('error_description') or 'Monarch would not register this app. Try again later.')
    _update_config(client_id=d['client_id'], client_redirect=REDIRECT, client_base=BASE)
    return d['client_id']


def login_start():
    meta = _meta()
    cid = _client_id(meta)
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(18)
    pending = [p for p in (_config().get('pending') or []) if _ago(p['at']) < 1800][-4:]
    pending.append({'state': state, 'verifier': verifier, 'at': _now()})
    _update_config(pending=pending)
    url = meta['authorization_endpoint'] + '?' + urlencode({
        'response_type': 'code', 'client_id': cid, 'redirect_uri': REDIRECT, 'scope': SCOPE, 'state': state,
        'code_challenge': _b64url(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256',
        'resource': RESOURCE})
    return {'url': url, 'return_address': REDIRECT}


def login_finish(pasted):
    text = str(pasted or '').strip()
    if not text or len(text) > 4000:
        raise Problem(400, 'Paste the whole address from your browser after you tapped Allow.')
    q = parse_qs(urlsplit(text).query) if '?' in text else {}
    if q.get('error'):
        raise Problem(400, 'Monarch said: ' + (q.get('error_description') or q['error'])[0][:200])
    code = (q.get('code') or [None])[0]
    state = (q.get('state') or [None])[0]
    if not code:
        if re.fullmatch(r'[A-Za-z0-9._~-]{8,512}', text):
            code = text
        else:
            raise Problem(400, "That address has no sign-in code in it. Copy the whole address after you tap Allow (it starts with http://127.0.0.1).")
    cfg = _config()
    pending = [p for p in (cfg.get('pending') or []) if _ago(p['at']) < 1800]
    match = [p for p in pending if p['state'] == state] if state else pending[-1:]
    if not match:
        raise Problem(409, 'That sign-in expired or was started elsewhere. Tap Sign in to Monarch again.')
    meta = _meta()
    st, _, b = _http('POST', meta['token_endpoint'], form={
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': REDIRECT, 'client_id': cfg['client_id'],
        'code_verifier': match[0]['verifier'], 'resource': RESOURCE})
    d = _json(b)
    if st != 200 or not d.get('access_token'):
        raise Problem(400, 'Monarch did not accept that code (' + str(d.get('error_description') or d.get('error') or st)[:160] + '). Tap Sign in to Monarch and try again; each code works once.')
    _store_tokens(d)
    _update_config(pending=None, connected_at=_now(), state='connected', last_error=None, rate_limited_until=None, backfilled=None)
    REMOTE_CLIENT.reset()
    threading.Thread(target=_safe_sync, kwargs={'full': True}, daemon=True).start()
    return status()


def _store_tokens(d, old=None):
    old = old or {}
    _save_tokens({'access_token': d['access_token'], 'refresh_token': d.get('refresh_token') or old.get('refresh_token'),
                  'expires_at': time.time() + float(d.get('expires_in') or 3600), 'scope': d.get('scope') or old.get('scope') or SCOPE})


def _access_token(force=False):
    t = _tokens()
    if not t.get('access_token'):
        raise Problem(409, 'Monarch is not connected. Open the Monarch app and sign in.')
    if not force and t.get('expires_at', 0) - 90 > time.time():
        return t['access_token']
    if not t.get('refresh_token'):
        _needs_login('Your Monarch sign-in expired.')
    meta = _meta()
    st, _, b = _http('POST', meta['token_endpoint'], form={
        'grant_type': 'refresh_token', 'refresh_token': t['refresh_token'], 'client_id': _config().get('client_id', ''), 'resource': RESOURCE})
    d = _json(b)
    if st == 200 and d.get('access_token'):
        _store_tokens(d, t)
        return d['access_token']
    if st in (400, 401):
        _needs_login('Monarch ended this sign-in (it may have been revoked in Monarch Settings).')
    raise MonarchError(st, 'refresh', "Couldn't refresh the Monarch sign-in right now. It will retry on the next sync.")


def _needs_login(msg):
    _save_tokens(None)
    _update_config(state='needs_login', last_error=msg + ' Sign in again in the Monarch app.')
    REMOTE_CLIENT.reset()
    raise Problem(409, msg + ' Sign in again in the Monarch app.')


def disconnect():
    t = _tokens()
    cfg = _config()
    revoked = False
    try:
        meta = _meta()
        if meta.get('revocation_endpoint') and t:
            for kind in ('refresh_token', 'access_token'):
                if t.get(kind):
                    st, _, _ = _http('POST', meta['revocation_endpoint'], form={'token': t[kind], 'token_type_hint': kind, 'client_id': cfg.get('client_id', '')})
                    revoked = revoked or st in (200, 204)
    except MonarchError:
        pass
    _save_tokens(None)
    REMOTE_CLIENT.reset()
    with LOCK:
        c = db()
        try:
            with c:
                c.execute('DELETE FROM transactions')
                c.execute('DELETE FROM accounts')
                c.execute('DELETE FROM snapshots')
        finally:
            c.close()
        cfg = _config()
        for k in ('state', 'last_sync', 'backfilled', 'last_error', 'connected_at', 'remote_tools', 'remote_tools_at', 'snapshot_at',
                  'rate_limited_until', 'history_from', 'syncing', 'pending'):
            cfg.pop(k, None)
        _save_config(cfg)
    return {**status(), 'revoked_at_monarch': revoked}


# ---------------- MCP client for Monarch's server ----------------

class Remote:
    def __init__(self):
        self.session, self.ready, self.seq = None, False, 0

    def reset(self):
        with REMOTE:
            self.session, self.ready = None, False

    def _post(self, msg, token):
        h = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
        if self.session:
            h['Mcp-Session-Id'] = self.session
        return _http('POST', MCP_URL, data=msg, headers=h, timeout=120)

    @staticmethod
    def _parse(headers, body, want):
        kind = (headers.get('Content-Type') or '').split(';')[0].strip().lower()
        msgs = []
        if kind == 'text/event-stream':
            buf = []
            for line in body.decode('utf-8', 'replace').splitlines() + ['']:
                if line.startswith('data:'):
                    buf.append(line[5:].lstrip())
                elif not line.strip() and buf:
                    try:
                        msgs.append(json.loads('\n'.join(buf)))
                    except ValueError:
                        pass
                    buf = []
        else:
            d = _json(body)
            msgs = d if isinstance(d, list) else [d]
        for m in msgs:
            if isinstance(m, dict) and m.get('id') == want:
                return m
        raise MonarchError(502, 'protocol', 'Monarch sent an answer this app could not read.')

    def _initialize(self, token):
        self.session = None
        self.seq += 1
        st, hd, b = self._post({'jsonrpc': '2.0', 'id': self.seq, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': APP_ID, 'version': VERSION}}}, token)
        if st == 401:
            return False
        if st >= 400:
            raise MonarchError(st, 'initialize', f'Monarch refused the connection ({st}).')
        self._parse(hd, b, self.seq)
        self.session = hd.get('Mcp-Session-Id')
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token)
        self.ready = True
        return True

    def request(self, method, params):
        with REMOTE:
            unauth = False
            for _ in range(3):
                token = _access_token(force=unauth)
                unauth = False
                if not self.ready and not self._initialize(token):
                    unauth = True
                    continue
                self.seq += 1
                want = self.seq
                st, hd, b = self._post({'jsonrpc': '2.0', 'id': want, 'method': method, 'params': params}, token)
                if st == 401:
                    unauth, self.ready = True, False
                    continue
                if st in (400, 404) and self.session:
                    self.ready = False  # the server dropped our session: start a fresh one
                    continue
                if st == 429:
                    raise MonarchError(429, 'rate_limited', RATE_MSG)
                if st >= 400:
                    err = _json(b)
                    raise MonarchError(st, 'http', str(err.get('error_description') or err.get('error') or f'Monarch answered {st}.')[:300])
                m = self._parse(hd, b, want)
                if m.get('error'):
                    text = str(m['error'].get('message') or m['error'])[:300]
                    if 'rate limit' in text.lower():
                        raise MonarchError(429, 'rate_limited', RATE_MSG)
                    raise MonarchError(502, 'rpc', text)
                return m.get('result') or {}
            _needs_login('Monarch stopped accepting this sign-in.')


REMOTE_CLIENT = Remote()


def remote_tools(refresh=False):
    cfg = _config()
    if not refresh and cfg.get('remote_tools') and _ago(cfg.get('remote_tools_at')) < 86400:
        return cfg['remote_tools']
    tools = REMOTE_CLIENT.request('tools/list', {}).get('tools') or []
    slim = [{'name': t.get('name'), 'description': (t.get('description') or '')[:600], 'inputSchema': t.get('inputSchema') or {},
             'readOnly': bool((t.get('annotations') or {}).get('readOnlyHint'))} for t in tools if t.get('name')]
    _update_config(remote_tools=slim, remote_tools_at=_now())
    return slim


def _rate_limited(msg):
    _update_config(rate_limited_until=time.time() + 24 * 3600, last_error=msg)


def _tool_args(name, args):
    # Generic task labels only: never send private conversation context as provider telemetry.
    out = dict(args or {})
    intents = {'GetBudget': 'budgeting', 'GetCashFlow': 'cash_flow_planning',
               'GetNetWorthHistory': 'net_worth_tracking', 'GetInvestments': 'investment_tracking',
               'GetRecurring': 'bills_and_subscriptions', 'GetGoals': 'savings_goals',
               'GetSpendingByCategory': 'spending_analysis'}
    out.setdefault('intent', intents.get(name, 'organizing_finances'))
    return out


def call_tool(name, args):
    args = _tool_args(name, args)
    cfg = _config()
    week = _week()
    calls = cfg.get('calls') if cfg.get('calls_week') == week else 0
    _update_config(calls=(calls or 0) + 1, calls_week=week)
    try:
        r = REMOTE_CLIENT.request('tools/call', {'name': name, 'arguments': args or {}})
    except MonarchError as e:
        if e.code == 'rate_limited':
            _rate_limited(e.message)
        raise
    text = ''.join(c.get('text', '') for c in (r.get('content') or []) if isinstance(c, dict) and c.get('type') == 'text')
    if r.get('isError'):
        if 'rate limit' in text.lower():
            _rate_limited(RATE_MSG)
            raise MonarchError(429, 'rate_limited', RATE_MSG)
        if 'scope' in text.lower():
            raise MonarchError(403, 'scope', 'That Monarch tool needs write access, which this connector does not ask for.')
        raise MonarchError(502, 'tool', f'{name}: {text[:300] or "Monarch returned an error."}')
    if isinstance(r.get('structuredContent'), (dict, list)):
        value = r['structuredContent']
    else:
        try:
            value = json.loads(text)
        except ValueError:
            value = {'text': text}
    # FastMCP wraps JSON-returning tools in a structuredContent.result string.
    if isinstance(value, dict) and set(value) == {'result'} and isinstance(value['result'], str):
        try:
            return json.loads(value['result'])
        except ValueError:
            pass
    return value


# ---------------- sync into the local copy ----------------

def _num(v):
    try:
        return round(float(v), 2) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _pick(d, *keys):
    for k in keys:
        cur = d
        for part in k.split('.'):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ''):
            return cur
    return None


def _text(v):
    return v if isinstance(v, str) else None


def _kind(type_name, is_asset):
    t = (type_name or '').lower()
    for word, kind in (('credit', 'credit'), ('loan', 'loan'), ('mortgage', 'loan'), ('invest', 'investment'), ('brokerage', 'investment'),
                       ('retire', 'investment'), ('401', 'investment'), ('ira', 'investment'), ('crypto', 'crypto'), ('real estate', 'property'),
                       ('property', 'property'), ('vehicle', 'vehicle'), ('check', 'depository'), ('saving', 'depository'), ('cash', 'depository'),
                       ('depository', 'depository')):
        if word in t:
            return kind
    return 'other' if is_asset in (None, True) else 'liability'


def _list_of(res, key):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        for k in (key, 'results', 'items', 'data'):
            if isinstance(res.get(k), list):
                return res[k]
    raise MonarchError(502, 'shape', f'Monarch answered without a {key} list; the connector needs an update.')


def _save_accounts(rows):
    with LOCK:
        c = db()
        try:
            with c:
                seen = []
                for a in rows:
                    aid = str(_pick(a, 'id', 'account_id') or '')
                    if not aid:
                        continue
                    seen.append(aid)
                    tname = _text(_pick(a, 'type', 'type.display', 'account_type'))
                    is_asset = _pick(a, 'is_asset', 'isAsset')
                    if is_asset is None:
                        is_asset = _kind(tname, None) not in ('credit', 'loan', 'liability')
                    c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        aid, _text(_pick(a, 'name', 'display_name', 'displayName')), _text(_pick(a, 'institution', 'institution.name')), tname,
                        _kind(tname, is_asset), _text(_pick(a, 'sub_type', 'subtype', 'subtype.display')), _text(_pick(a, 'mask', 'last_four')),
                        None if is_asset is None else int(bool(is_asset)), _num(_pick(a, 'balance', 'display_balance', 'displayBalance')),
                        _num(_pick(a, 'signed_balance', 'current_balance', 'currentBalance')), _num(_pick(a, 'credit_limit', 'limit')),
                        int(bool(_pick(a, 'closed'))), int(bool(_pick(a, 'is_hidden', 'isHidden'))), json.dumps(a)[:20000], _now()))
                if seen:
                    c.execute(f'DELETE FROM accounts WHERE id NOT IN ({",".join("?" * len(seen))})', seen)
        finally:
            c.close()
    return len(rows)


def _save_transactions(rows, start, end, complete):
    ids = []
    with LOCK:
        c = db()
        try:
            with c:
                for t in rows:
                    tid = str(_pick(t, 'id', 'transaction_id') or '')
                    if not tid:
                        continue
                    ids.append(tid)
                    merchant = _text(_pick(t, 'merchant', 'merchant.name'))
                    c.execute('INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                        tid, _text(_pick(t, 'account_id', 'account.id')), _text(_pick(t, 'date')), _num(_pick(t, 'amount')), merchant,
                        _text(_pick(t, 'original_statement', 'plaid_name', 'description')) or merchant, _text(_pick(t, 'category', 'category.name')),
                        _text(_pick(t, 'category_group', 'category.group.name')), _text(_pick(t, 'category_type', 'category.group.type')),
                        int(bool(_pick(t, 'pending', 'is_pending'))), json.dumps(t)[:8000], _now()))
                if complete:  # Monarch is the source of truth for this window: drop what it deleted or merged
                    if ids:
                        c.execute(f'DELETE FROM transactions WHERE date >= ? AND date <= ? AND id NOT IN ({",".join("?" * len(ids))})', [start, end, *ids])
                    else:
                        c.execute('DELETE FROM transactions WHERE date >= ? AND date <= ?', (start, end))
        finally:
            c.close()
    return len(ids)


def _pull(start, end, depth=0):
    # Official MCP now caps pages at 100 and returns next_offset.
    rows, offset, expected = [], 0, None
    while True:
        res = call_tool('GetTransactions', {'start_date': start.isoformat(), 'end_date': end.isoformat(),
                                          'limit': min(PAGE, 100), 'offset': offset, 'sort_by': 'date'})
        batch = _list_of(res, 'transactions')
        total = res.get('total_count') if isinstance(res, dict) else None
        if isinstance(total, int):
            expected = total
        rows.extend(batch)
        nxt = res.get('next_offset') if isinstance(res, dict) else None
        if nxt is None:
            if expected is not None and len(rows) < expected:
                if not batch:
                    raise MonarchError(502, 'pagination', 'Monarch ended transaction paging before the total was reached.')
                nxt = offset + len(batch)
            else:
                break
        if not isinstance(nxt, int) or nxt <= offset or not batch:
            raise MonarchError(502, 'pagination', 'Monarch returned an invalid transaction page cursor.')
        offset = nxt
    ids = [str(_pick(t, 'id', 'transaction_id') or '') for t in rows]
    if len(set(ids)) != len(rows) or '' in ids or (expected is not None and len(rows) != expected):
        raise MonarchError(502, 'pagination', 'Monarch transaction pages did not match the reported total; retry the sync.')
    return _save_transactions(rows, start.isoformat(), end.isoformat(), complete=True)


def _fill_args(schema, kind):
    props = (schema or {}).get('properties') or {}
    req = (schema or {}).get('required') or []
    today = dt.date.today()
    out = _tool_args(SNAPSHOTS.get(kind, ''), {})
    for name in props:
        n = name.lower()
        if 'start' in n and 'date' in n:
            out[name] = (today - dt.timedelta(days=365)).isoformat() if kind in ('net_worth', 'investments') else today.replace(day=1).isoformat()
        elif 'end' in n and 'date' in n:
            out[name] = today.isoformat()
    return out, [r for r in req if r not in out]


def _snapshots(tools):
    by = {t['name']: t for t in tools}
    for kind, tool in SNAPSHOTS.items():
        if tool not in by:
            continue
        args, missing = _fill_args(by[tool].get('inputSchema'), kind)
        data, err = None, None
        if missing:
            err = 'needs ' + ', '.join(missing)
        else:
            try:
                data = call_tool(tool, args)
            except MonarchError as e:
                if e.code == 'rate_limited':
                    raise
                err = e.message
        with LOCK:
            c = db()
            try:
                with c:
                    if data is not None:
                        c.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)', (kind, _now(), json.dumps(data)[:2_000_000], None))
                    else:
                        c.execute('INSERT INTO snapshots(kind,at,data,error) VALUES (?,?,NULL,?) ON CONFLICT(kind) DO UPDATE SET error=excluded.error', (kind, _now(), err))
            finally:
                c.close()
    _update_config(snapshot_at=_now())


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        raise Problem(409, 'A sync is already running.')
    try:
        cfg = _config()
        if not _tokens().get('access_token'):
            raise Problem(409, 'Monarch is not connected. Open the Monarch app and sign in.')
        if (cfg.get('rate_limited_until') or 0) > time.time() and not full:
            raise Problem(429, RATE_MSG)
        _update_config(syncing=_now())
        tools = remote_tools(refresh=full)
        names = {t['name'] for t in tools}
        for need in ('GetAccounts', 'GetTransactions'):
            if need not in names:
                raise MonarchError(502, 'contract', f"Monarch's MCP no longer offers {need}; the connector needs an update.")
        accts = _list_of(call_tool('GetAccounts', {}), 'accounts')
        _save_accounts(accts)
        today = dt.date.today()
        months = int(cfg.get('history_months') or 24)
        backfill = full or not cfg.get('backfilled')
        start = (today - dt.timedelta(days=round(months * 30.44))) if backfill else today - dt.timedelta(days=RECENT_DAYS)
        n, cur = 0, start
        while cur <= today:  # quarter windows, split further when one holds more than a page
            end = min(cur + dt.timedelta(days=91), today)
            n += _pull(cur, end)
            cur = end + dt.timedelta(days=1)
        if backfill or _ago(cfg.get('snapshot_at')) > SNAPSHOT_HOURS * 3600:
            _snapshots(tools)
        upd = {'last_sync': _now(), 'state': 'connected', 'last_error': None, 'rate_limited_until': None, 'syncing': None}
        if backfill:
            upd.update(backfilled=_now(), history_from=start.isoformat())
        _update_config(**upd)
        return {'synced': {'accounts': len(accts), 'transactions_seen': n, 'from': start.isoformat()}, **status()}
    except MonarchError as e:
        _update_config(syncing=None, last_error=e.message)
        raise
    except Problem:
        _update_config(syncing=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full=full)
    except (Problem, MonarchError):
        pass
    except Exception as e:  # noqa: BLE001 - background sync must not kill the app
        _update_config(syncing=None, last_error=f'Sync failed: {type(e).__name__}')


def _scheduler():
    while True:
        time.sleep(60)
        cfg = _config()
        if not _tokens().get('access_token') or (cfg.get('rate_limited_until') or 0) > time.time():
            continue
        if _ago(cfg.get('last_sync')) > SYNC_HOURS * 3600 and _ago(cfg.get('last_attempt')) > 3600:
            _update_config(last_attempt=_now())
            _safe_sync()


# ---------------- read API (same contract as the Teller connector) ----------------

def accounts():
    with LOCK:
        c = db()
        try:
            rows = [dict(r) for r in c.execute('SELECT id,name,institution,type,kind,subtype,mask,is_asset,balance,signed_balance,credit_limit,closed,hidden,seen FROM accounts ORDER BY institution,name')]
        finally:
            c.close()
    return [{'id': a['id'], 'institution': a['institution'] or 'Manual', 'name': a['name'], 'type': a['kind'], 'subtype': a['subtype'],
             'last_four': a['mask'], 'currency': 'USD', 'status': 'closed' if a['closed'] else 'open',
             'available': a['balance'] if a['kind'] == 'depository' else None, 'ledger': a['balance'], 'balance_at': a['seen'],
             'monarch_type': a['type'], 'is_asset': None if a['is_asset'] is None else bool(a['is_asset']),
             'signed_balance': a['signed_balance'], 'credit_limit': a['credit_limit'], 'hidden': bool(a['hidden'])} for a in rows]


def _snapshot_kinds():
    with LOCK:
        c = db()
        try:
            return {r[0] for r in c.execute('SELECT kind FROM snapshots WHERE data IS NOT NULL')}
        finally:
            c.close()


def status():
    cfg = _config()
    tok = _tokens()
    accts = accounts()
    with LOCK:
        c = db()
        try:
            n = c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
            span = c.execute('SELECT MIN(date), MAX(date) FROM transactions').fetchone()
        finally:
            c.close()
    insts = {}
    for a in accts:
        insts.setdefault(a['institution'], []).append(a)
    if tok.get('access_token'):
        state = 'rate_limited' if (cfg.get('rate_limited_until') or 0) > time.time() else 'connected'
    else:
        state = 'needs_login' if cfg.get('state') == 'needs_login' else 'not_connected'
    return {
        'contract': 'wizard.bank-connection/1', 'provider': 'monarch', 'name': 'Monarch',
        'status': state, 'syncing': bool(cfg.get('syncing')) and _ago(cfg.get('syncing')) < 1800,
        'scope': tok.get('scope') if tok else None, 'connected_at': cfg.get('connected_at'),
        'last_sync': cfg.get('last_sync'), 'last_error': cfg.get('last_error'),
        'sync_every_hours': SYNC_HOURS, 'history_months': int(cfg.get('history_months') or 24),
        'banks': [{'id': re.sub(r'[^a-z0-9]+', '-', k.lower()).strip('-') or 'manual', 'institution': k, 'status': 'connected',
                   'synced': cfg.get('last_sync'), 'accounts': v} for k, v in insts.items()],
        'transactions': n, 'transactions_from': span[0], 'transactions_to': span[1],
        'calls_this_week': cfg.get('calls') if cfg.get('calls_week') == _week() else 0,
        'snapshots': sorted(_snapshot_kinds()),
    }


def transactions(args):
    q, p = ['SELECT t.id,t.account,a.institution,a.name AS account_name,a.mask AS last_four,t.date,t.amount,t.description,'
            't.merchant AS counterparty,t.category,t.category_group,t.category_type,'
            "CASE t.pending WHEN 1 THEN 'pending' ELSE 'posted' END AS status FROM transactions t LEFT JOIN accounts a ON a.id=t.account WHERE 1=1"], []
    for key, col, op in (('from', 't.date', '>='), ('to', 't.date', '<=')):
        v = args.get(key)
        if v:
            if not DATE_RE.match(str(v)):
                raise Problem(400, f'{key} must be YYYY-MM-DD.')
            q.append(f'AND {col} {op} ?'); p.append(v)
    if args.get('account'):
        a = str(args['account'])[:60]
        q.append('AND (t.account = ? OR a.mask = ? OR a.name LIKE ?)'); p += [a, a, '%' + a + '%']
    if args.get('search'):
        s = '%' + str(args['search'])[:80] + '%'
        q.append('AND (t.description LIKE ? OR t.merchant LIKE ?)'); p += [s, s]
    if args.get('category'):
        s = str(args['category'])[:80]
        q.append('AND (t.category LIKE ? OR t.category_group LIKE ?)'); p += [s, s]
    for key, op in (('min_amount', '>='), ('max_amount', '<=')):
        if args.get(key) not in (None, ''):
            q.append(f'AND t.amount {op} ?'); p.append(float(args[key]))
    if args.get('status') in ('posted', 'pending'):
        q.append('AND t.pending = ?'); p.append(1 if args['status'] == 'pending' else 0)
    limit = max(1, min(int(args.get('limit') or 100), 1000))
    q.append('ORDER BY t.date DESC, t.id DESC LIMIT ?'); p.append(limit)
    with LOCK:
        c = db()
        try:
            rows = [dict(r) for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    return {'count': len(rows), 'transactions': rows,
            'note': "Monarch amounts: negative = money out (spending), positive = money in. Categories are Monarch's, as of the last sync."}


def snapshot(kind):
    if kind not in SNAPSHOTS:
        raise Problem(400, 'kind must be one of: ' + ', '.join(SNAPSHOTS))
    with LOCK:
        c = db()
        try:
            r = c.execute('SELECT at,data,error FROM snapshots WHERE kind=?', (kind,)).fetchone()
        finally:
            c.close()
    if not r:
        return {'kind': kind, 'at': None, 'data': None, 'note': 'Not fetched yet; it refreshes daily with sync.'}
    return {'kind': kind, 'at': r['at'], 'data': json.loads(r['data']) if r['data'] else None, 'error': r['error']}


def live_call(args):
    tools = remote_tools()
    name = args.get('tool')
    if not name:
        return {'tools': [{'name': t['name'], 'read_only': t['readOnly'], 'description': t['description'][:240]} for t in tools],
                'calls_this_week': status()['calls_this_week']}
    t = next((t for t in tools if t['name'] == name), None)
    if not t:
        raise Problem(404, f'Monarch has no tool named {name}. Call without tool to list them.')
    if not (t['readOnly'] or re.match(r'^(Get|List)[A-Z]', name)):
        raise Problem(403, 'This connector is read-only; it only passes through Monarch read tools.')
    a = args.get('arguments') or {}
    if not isinstance(a, dict):
        raise Problem(400, 'arguments must be an object.')
    return {'tool': name, 'result': call_tool(name, a)}


def save_settings(body):
    if 'history_months' in body:
        m = int(body['history_months'])
        if not 1 <= m <= 120:
            raise Problem(400, 'history_months must be 1 to 120.')
        _update_config(history_months=m, backfilled=None)
    return status()


TOOLS = [
    {'name': 'bank_status', 'description': 'Monarch connection status: sign-in state, accounts grouped by institution, last sync, transactions held locally, Monarch MCP calls used this week. Never returns tokens. To connect, the owner opens the Monarch app (/wizard-monarch/); never ask for Monarch credentials or codes in chat.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'bank_accounts', 'description': 'All Monarch accounts (banks, cards, loans, investments, manual) with institution, name, last four, type and current balance, from the local copy.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'bank_transactions', 'description': "Search synced Monarch transactions, newest first, with Monarch's merchant and category. Filters: from/to (YYYY-MM-DD), account (id, last four or name), search (merchant/description), category (category or group name), min_amount/max_amount, status (posted/pending), limit (default 100, max 1000). Reads the local copy; costs no Monarch quota.",
     'inputSchema': {'type': 'object', 'properties': {'from': {'type': 'string'}, 'to': {'type': 'string'}, 'account': {'type': 'string'}, 'search': {'type': 'string'}, 'category': {'type': 'string'},
                     'min_amount': {'type': 'number'}, 'max_amount': {'type': 'number'}, 'status': {'type': 'string', 'enum': ['posted', 'pending']}, 'limit': {'type': 'integer'}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'bank_sync', 'description': 'Pull the latest accounts and recent transactions from Monarch now (it also syncs every 12 hours). Uses a few calls of the weekly Monarch quota.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
    {'name': 'monarch_snapshot', 'description': "Monarch's own budget (this month), cash flow (this month), net worth history (12 months), recurring bills, categories or investments, as of the last daily refresh. kind: budget|cashflow|net_worth|recurring|categories|investments.",
     'inputSchema': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': list(SNAPSHOTS)}}, 'required': ['kind'], 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'monarch_call', 'description': "Live read-only pass-through to Monarch's official MCP tools (GetSpendingByCategory, GetGoals, GetMerchants, GetCreditScoreHistory, ...). Omit tool to list them with their descriptions. Each call uses the weekly Monarch quota, so prefer bank_transactions and monarch_snapshot for routine questions.",
     'inputSchema': {'type': 'object', 'properties': {'tool': {'type': 'string'}, 'arguments': {'type': 'object'}}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a, n = p.get('arguments') or {}, p.get('name')
            if n == 'bank_status':
                v = status()
            elif n == 'bank_accounts':
                v = {'accounts': accounts()}
            elif n == 'bank_transactions':
                v = transactions(a)
            elif n == 'bank_sync':
                v = sync()
            elif n == 'monarch_snapshot':
                v = snapshot(a.get('kind'))
            elif n == 'monarch_call':
                v = live_call(a)
            else:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, MonarchError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'], 'accounts': sum(len(b['accounts']) for b in s['banks'])})
            if path == '/api/status':
                return self.send(200, status())
            if path == '/api/accounts':
                return self.send(200, {'accounts': accounts()})
            if path == '/api/transactions':
                return self.send(200, transactions(query))
            if path == '/api/snapshot':
                return self.send(200, snapshot(query.get('kind')))
            if path == '/oauth/callback':  # works only when the browser runs on this box itself
                try:
                    login_finish('http://127.0.0.1' + self.path)
                    return self.send(302, b'', 'text/plain', {'Location': PREFIX + '/' if self.path.startswith(PREFIX) else '/'})
                except (Problem, MonarchError) as e:
                    return self.send(400, e.message.encode(), 'text/plain; charset=utf-8')
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if any(self.headers.get(h) for h in FORWARDED):
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/login/start':
                return self.send(200, login_start())
            if path == '/api/login/finish':
                return self.send(200, login_finish(b.get('url')))
            if path == '/api/sync':
                return self.send(200, sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect())
            if path == '/api/settings':
                return self.send(200, save_settings(b))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except MonarchError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.umask(0o077)
    # The App Manager may loosen app-data permissions while the app is stopped; tighten them again on start.
    for f in _private().rglob('*'):
        try:
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        except OSError:
            pass
    db().close()
    if _config().get('syncing'):
        _update_config(syncing=None)
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()
