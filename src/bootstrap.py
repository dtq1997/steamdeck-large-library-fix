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

PAYLOAD = '__LLF_PAYLOAD__'
FILES = {'engine.py', 'rules.json', 'install.py', 'status.py', 'manager.py', 'README.md', 'LICENSE'}


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
        path.chmod(0o600)


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
            unpack(PAYLOAD, stage)
            return subprocess.run([sys.executable, str(stage / 'manager.py'), '--install']).returncode
    except Exception as error:
        subprocess.run(['kdialog', '--title', 'Steam Library Fix', '--error',
                        'Installer could not start. / 安装器未能启动。\n' + str(error)])
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
