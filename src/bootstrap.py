#!/usr/bin/env python3
"""Embedded offline payload loader; generated launchers contain this source."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse

PAYLOAD = ''
FILES = {'engine.py', 'rules.json', 'install.py', 'status.py', 'manager.py', 'README.md', 'LICENSE',
         'compat.py', 'compatibility.json', 'syntax.js', 'acorn.js', 'terser.js',
         'qjs-linux-x86_64', 'vendor.json', 'THIRD-PARTY-NOTICES.txt'}
FILES.update({'native_syntax.py', 'libsyntax-linux-x86_64.so'})


def read_payload(filename):
    if filename.startswith('file:'):
        value = urlparse(filename)
        if value.netloc not in {'', 'localhost'}:
            raise ValueError('Installer must be a local file')
        filename = unquote(value.path)
    path = Path(filename)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Installer exceeds size limit')
    values = [line.split('=', 1)[1] for line in path.read_text().splitlines()
              if line.startswith('X-LLF-Payload=')]
    if len(values) != 1:
        raise ValueError('Installer payload missing or ambiguous')
    return values[0]


def unpack(payload, directory):
    bundle = json.loads(zlib.decompress(base64.b64decode(payload, validate=True)))
    if set(bundle) != FILES:
        raise ValueError('Unexpected installer contents')
    decoded = {}
    for name, record in bundle.items():
        data = base64.b64decode(record['data'], validate=True)
        if hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('Installer integrity check failed: ' + name)
        decoded[name] = data
    for name, data in decoded.items():
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o700 if name == 'qjs-linux-x86_64' else 0o600)


def main():
    try:
        if os.geteuid() == 0:
            raise ValueError('Open this installer as your normal Deck user, without sudo.')
        cache = Path.home() / '.cache/steam-large-library-fix'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache.is_symlink():
            raise ValueError('Installer cache must not be a symbolic link')
        with tempfile.TemporaryDirectory(prefix='installer-', dir=cache) as name:
            stage = Path(name)
            unpack(PAYLOAD or read_payload(sys.argv[1]), stage)
            return subprocess.run([sys.executable, str(stage / 'manager.py'), '--install']).returncode
    except Exception as error:
        subprocess.run(['kdialog', '--title', 'Steam Library Fix', '--error',
                        'Installer could not start. / 安装器未能启动。\n' + str(error)])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
