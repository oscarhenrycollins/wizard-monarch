"""Release smoke with a synthetic HTTP provider; never uses owner credentials."""
import importlib.util
import json
import os
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('connector', root / 'app/server.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.send({key: base + '/' + path for key, path in [
            ('authorization_endpoint', 'authorize'), ('registration_endpoint', 'register'),
            ('token_endpoint', 'token'), ('revocation_endpoint', 'revoke')]})
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/register':
            assert json.loads(raw)['scope'] == 'mcp:read'
            return self.send({'client_id': 'synthetic-client'}, 201)
        if self.path == '/token':
            return self.send({'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
                              'expires_in': 3600, 'scope': 'mcp:read'})
        if self.path == '/revoke': return self.send({})
        d = json.loads(raw)
        method = d['method']
        if method == 'notifications/initialized': return self.send({})
        if method == 'initialize': result = {'protocolVersion': m.PROTOCOL, 'capabilities': {}}
        elif method == 'tools/list':
            result = {'tools': [{'name': name, 'inputSchema': {'properties': {'intent': {}}, 'required': ['intent']},
                                 'annotations': {'readOnlyHint': True}} for name in ('GetAccounts', 'GetTransactions')]}
        else:
            args, name = d['params']['arguments'], d['params']['name']
            assert args['intent'] == 'organizing_finances'
            assert name in ('GetAccounts', 'GetTransactions')
            if name == 'GetAccounts':
                data = {'accounts': [{'id': 'fixture-account', 'name': 'Synthetic account', 'institution': 'Fixture Bank', 'type': 'checking', 'balance': 12}]}
            else:
                assert args['limit'] <= 100
                data = {'transaction_count': 1, 'next_offset': None, 'transactions': [
                    {'id': 'fixture-tx-' + args['start_date'], 'account_id': 'fixture-account', 'date': args['start_date'],
                     'amount': -1, 'merchant': 'Synthetic store', 'category': 'Fixture category'}]}
            result = {'structuredContent': {'result': json.dumps(data)}}
        self.send({'jsonrpc': '2.0', 'id': d['id'], 'result': result})

with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as folder:
    m.DATA = Path(folder)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    m.BASE = 'http://127.0.0.1:' + str(provider.server_port)
    m.MCP_URL = m.BASE + '/mcp'
    m._safe_sync = lambda **kw: None  # sync explicitly so assertions are synchronous
    server = ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    m.TRUSTED_HOSTS.add('127.0.0.1:' + str(server.server_port))
    def api(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.load(r)
        except urllib.error.HTTPError as e: return e.code, json.load(e)
    assert api('/api/connection')[1]['status'] == 'not_connected'
    assert api('/api/login/finish', {'url': 'invalid'})[0] == 400
    start = api('/api/login/start', {})[1]
    state = parse_qs(urlsplit(start['url']).query)['state'][0]
    assert parse_qs(urlsplit(start['url']).query)['scope'] == ['mcp:read']
    assert api('/api/login/finish', {'url': m.REDIRECT + '?code=synthetic&state=wrong'})[0] == 409
    assert api('/api/login/finish', {'url': m.REDIRECT + '?code=synthetic&state=' + state})[0] == 200
    m.save_settings({'history_months': 1})
    assert m.sync(full=True)['synced']['accounts'] == 1
    assert api('/api/transactions?search=Synthetic')[1]['count'] >= 1
    c = m.db(); n = c.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]; c.close()
    assert n > 0
    # Re-load module to prove private token/cache persistence independent of process memory.
    m2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m2); m2.DATA = m.DATA
    assert len(m2.accounts()) == 1 and m2._tokens()['scope'] == 'mcp:read'
    for path in ('/api/connection', '/api/status', '/healthz'):
        text = json.dumps(api(path)[1])
        assert 'synthetic-access' not in text and 'synthetic-refresh' not in text
    conn = api('/api/connection')[1]
    assert set(conn) == {'status', 'access', 'last_sync', 'last_error', 'syncing', 'version'}
    msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    assert api('/mcp', msg, {'X-Forwarded-For': '192.0.2.1'})[0] == 403
    assert api('/api/review/apply', {'transaction_id': 'fixture'})[0] == 404
    assert api('/api/settings', {}, {'Origin': 'https://untrusted.invalid'})[0] == 403
    for name in ('UpdateTransaction', 'CreateMerchant', 'DeleteTransaction'):
        try: m.call_tool(name, {}); raise AssertionError('write passed')
        except m.Problem as e: assert e.code == 403
    assert (m.DATA.stat().st_mode & 0o777) == 0o700
    assert (m.DATA / 'tokens.json').stat().st_mode & 0o777 == 0o600
    assert api('/api/disconnect', {})[1]['status'] == 'not_connected'
    assert not m._tokens() and not m.accounts() and m.transactions({})['count'] == 0
    server.shutdown(); provider.shutdown()
print('PASS: fake HTTP OAuth/PKCE, nonce refusal, read-only scope, sync, search, reload persistence, secret-free status, host/origin/MCP guards, no write path, permissions, disconnect')
