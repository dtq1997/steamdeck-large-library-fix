#!/usr/bin/env python3
"""Build a deterministic offline .desktop installer and an allowlisted source ZIP."""
import argparse
import base64
import hashlib
import json
import zipfile
import zlib
from pathlib import Path

import engine
from install import PAYLOAD

HERE = Path(__file__).resolve().parent
SOURCES = (*PAYLOAD, 'bootstrap.py', 'build_release.py', 'install.sh', 'test_packaging.py',
           'generate_compat.py', 'test_compat.py', 'test_local.py', 'test_model.py', 'build_native.py', 'test_updater.py')
ROOT = HERE.parent
DOCUMENTS = ('README.md', 'README.zh-CN.md', 'LICENSE')
ARCHIVE_FILES = (*DOCUMENTS, 'BUILDING.md', '.gitignore',
                 *('src/' + name for name in SOURCES))


def encoded_payload():
    bundle = {name: {'data': base64.b64encode((HERE / name).read_bytes()).decode('ascii'),
                     'sha256': hashlib.sha256((HERE / name).read_bytes()).hexdigest()}
              for name in sorted(PAYLOAD)}
    return base64.b64encode(zlib.compress(json.dumps(bundle, sort_keys=True).encode(), 9)).decode('ascii')


def desktop_source():
    bootstrap = (HERE / 'bootstrap.py').read_text()
    code = base64.b64encode(zlib.compress(bootstrap.encode(), 9)).decode('ascii')
    command = "import base64,zlib;exec(zlib.decompress(base64.b64decode('" + code + "')))"
    assert len(command) < 100000 and not any(c in command for c in '%$`\\\"')
    return f'''#!/usr/bin/env xdg-open
[Desktop Entry]
Version=1.0
Type=Application
Name=Install Steam Library Fix
Name[zh_CN]=安装 Steam 大库优化
Comment=Offline installer {engine.VERSION}; no administrator password required
Comment[zh_CN]=离线安装器 {engine.VERSION}，无需管理员密码
Exec=/usr/bin/python3 -c "{command}" %k
Icon=steam
Terminal=false
Categories=Utility;
StartupNotify=false
X-LLF-Payload={encoded_payload()}
'''


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    stem = 'SteamDeck-LargeLibraryFix-' + engine.VERSION
    desktop = destination / (stem + '.desktop')
    desktop.write_text(desktop_source())
    desktop.chmod(0o755)
    archive = destination / (stem + '-source.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(ARCHIVE_FILES):
            info = zipfile.ZipInfo(stem + '/' + name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith('.sh') or name.endswith('qjs-linux-x86_64') else 0o100644) << 16
            output.writestr(info, (ROOT / name).read_bytes())
    for name in DOCUMENTS:
        (destination / name).write_bytes((ROOT / name).read_bytes())
    files = [desktop, archive, *(destination / name for name in DOCUMENTS)]
    checksums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files)
    (destination / 'SHA256SUMS').write_text(checksums)
    print(json.dumps({'version': engine.VERSION, 'installer': str(desktop),
                      'bytes': desktop.stat().st_size, 'source_files': sorted(ARCHIVE_FILES)}, ensure_ascii=False))
    return desktop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    build(args.output.resolve())


if __name__ == '__main__':
    main()
