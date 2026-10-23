"""Package: run `wam package-inline .`, then apply this app's runtime specifics (port, pinned base image,
process limit). Run from the repo root:  python3 tools/package.py"""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
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
p.write_text(t)
print('packaged', p.relative_to(root))
