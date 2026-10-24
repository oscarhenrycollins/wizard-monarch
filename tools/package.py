"""Package: run `wam package-inline .`, then apply this app's runtime specifics (port, pinned base image,
process limit). Run from the repo root:  python3 tools/package.py"""
import re
import subprocess
import sys
import base64
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if '--check' in sys.argv:
    import yaml
    compose = yaml.safe_load((root / 'runtipi/docker-compose.yml').read_text())
    command = compose['services']['wizard-monarch']['command'][-1]
    assert len(command.encode()) < 120000, 'inline runtime argument exceeds safe size'
    payload = re.search(r"base64.b64decode\('([A-Za-z0-9+/=]+)'\)", command)
    assert payload, 'inline source payload missing'
    files = json.loads(base64.b64decode(payload.group(1)))
    for name, text in files.items():
        assert (root / name).read_text() == text, f'stale packaged source: {name}'
    assert 'app/server.py' in files and 'web/app.js' in files
    print(f'PASS: {len(files)} embedded runtime files match source; argument {len(command.encode())} bytes')
    raise SystemExit(0)
subprocess.run(['wam', 'package-inline', str(root)], check=True, stdout=subprocess.DEVNULL)
p = root / 'runtipi' / 'docker-compose.yml'
t = p.read_text()
swaps = [
    ('    image: python:3.12-alpine\n',
     '    image: python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4\n    pids_limit: 64\n'),
    ("      PORT: '8799'\n", "      PORT: '8806'\n      BIND: 0.0.0.0\n      PYTHONDONTWRITEBYTECODE: '1'\n"),
    ('      internal_port: 8799\n', '      internal_port: 8806\n'),
    ('      DATA_DIR: /data\n', '      DATA_DIR: /data/private\n'),
]
for old, new in swaps:
    if t.count(old) != 1:
        raise SystemExit(f'package template changed; expected one {old.strip()!r}, found {t.count(old)}')
    t = t.replace(old, new)
assert '/data/private' in t and '@sha256:' in t and '8799' not in t
assert max(map(len, t.splitlines())) < 120000, 'compress payload before shipping'
p.write_text(t)
print('packaged', p.relative_to(root))
