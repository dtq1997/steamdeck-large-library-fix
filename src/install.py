#!/usr/bin/env python3
"""Install/remove user-owned maintenance hooks; no root or client restarts."""
import argparse
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import engine

MARKER = "# Managed by steam-large-library-fix\n"
PAYLOAD = ("engine.py", "rules.json", "install.py", "status.py", "manager.py", "README.md", "LICENSE",
           'compat.py', 'compatibility.json', 'syntax.js', 'acorn.js', 'terser.js',
           'qjs-linux-x86_64', 'vendor.json', 'THIRD-PARTY-NOTICES.txt')
PAYLOAD += ('native_syntax.py', 'libsyntax-linux-x86_64.so', 'local.js', 'model.js', 'updater.py')
PAYLOAD += ('structural.py', 'structural.js', 'operations.js')


def systemctl(*args):
    subprocess.run(["systemctl", "--user", *args], check=True, timeout=25)


def desktop_dir(home):
    try:
        value = subprocess.run(["xdg-user-dir", "DESKTOP"], check=True, timeout=3,
                               text=True, capture_output=True).stdout.strip()
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(home):
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return home / "Desktop"


def control_files(home, installed):
    directory = home / ".config/systemd/user"
    program = '%h/.local/share/steam-large-library-fix/versions/' + engine.VERSION + '/engine.py'
    command = '/usr/bin/python3 "' + program + '" apply --automatic '
    desktop = desktop_dir(home) / "Steam 大库修复状态.desktop"
    helper = str(installed / "manager.py").replace("\\", "\\\\").replace('"', '\\"')
    return {
        directory / "steam-large-library-fix.service": MARKER + f'''[Unit]
Description=Check supported Steam library code
StartLimitIntervalSec=0
Before=steam-launcher.service

[Service]
Type=oneshot
ExecStartPre=/usr/bin/sleep 1
ExecStart={command}--reason=file-change
TimeoutStartSec=90
Restart=no
RestartForceExitStatus=75
RestartSec=3

[Install]
WantedBy=default.target
''',
        directory / "steam-large-library-fix.path": MARKER + '''[Unit]
Description=Notice Steam UI file replacements

[Path]
PathChanged=%h/.local/share/Steam/steamui
PathChanged=%h/.local/share/Steam/steamui/sp.js
PathChanged=%h/.local/share/Steam/steamui/library.js
PathChanged=%h/.local/share/Steam/steamui/css/sp.css
Unit=steam-large-library-fix.service
TriggerLimitIntervalSec=0

[Install]
WantedBy=default.target
''',
        directory / "steam-launcher.service.d/40-large-library-fix.conf": MARKER + f'''[Service]
ExecStartPre=-{command}--reason=steam-start
''',
        desktop: MARKER + f'''[Desktop Entry]
Type=Application
Name=Steam Library Fix
Name[zh_CN]=Steam 大库优化
Comment=Check status, repair or uninstall the library patch
Comment[zh_CN]=查看状态、修复或卸载大库补丁
Exec=/usr/bin/python3 "{helper}"
Icon=steam
Terminal=false
''',
    }


def preflight(files, previous):
    known = previous.get("control_hashes", {})
    for path, content in files.items():
        if not path.exists():
            continue
        data = path.read_bytes()
        if path.is_symlink() or (data != content.encode() and engine.sha(data) != known.get(str(path))):
            raise ValueError("Existing user-edited control file left untouched: " + str(path))


def stage_version(installed):
    source = Path(__file__).resolve().parent
    if installed.exists():
        for name in PAYLOAD:
            if not (installed / name).is_file() or (installed / name).read_bytes() != (source / name).read_bytes():
                raise ValueError("Installed version differs; no overwrite: " + str(installed))
        return
    staging = installed.with_name(".stage-" + str(os.getpid()))
    staging.mkdir(parents=True)
    try:
        for name in PAYLOAD:
            shutil.copy2(source / name, staging / name)
        os.replace(staging, installed)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


@contextmanager
def pause_maintenance(previous):
    # A queued old-version checker must not undo the new patch during upgrade.
    managed = bool(previous.get('control_hashes'))
    try:
        if managed:
            systemctl('stop', 'steam-large-library-fix.path', 'steam-large-library-fix.service')
        yield
    except BaseException:
        if managed:
            systemctl('daemon-reload')
            systemctl('start', 'steam-large-library-fix.path')
        raise


def install(home, seed):
    if os.geteuid() == 0:
        raise ValueError("Run as the Steam Deck user, without sudo")
    if not Path("/usr/lib/steamos/steam-launcher").is_file():
        raise ValueError("This release supports native SteamOS Game Mode only")
    state = home / ".local/state/steam-large-library-fix"
    ui = home / ".local/share/Steam/steamui"
    installed = home / ".local/share/steam-large-library-fix/versions" / engine.VERSION
    record_path = state / "installation.json"
    previous = json.loads(record_path.read_text()) if record_path.exists() else {}
    files = control_files(home, installed)
    preflight(files, previous)
    stage_version(installed)
    rules = json.loads((installed / "rules.json").read_text())
    with pause_maintenance(previous):
        result = finish_install(ui, state, rules, seed, files, record_path)
    print("已安装。待机、正常重启及兼容代码更新后会自动检查。首次安装后请正常重开 Steam。")
    print("桌面“Steam 大库优化”可查看状态或卸载。未知内部实现不会被强行修改。")
    if result['status'] == 'partial':
        print('部分优化因相关代码变化暂停：' + ', '.join(result['paused']))


def finish_install(ui, state, rules, seed, files, record_path):
    result = engine.execute("apply", ui.resolve(), state, rules, seed, "install")
    if result["status"] not in {'applied', 'partial'}:
        raise ValueError("Not installed: " + result.get("detail", result["status"]))
    for path, content in files.items():
        engine.atomic_write(path, content.encode(), 0o755 if path.suffix == ".desktop" else 0o644)
    engine.save_json(record_path, {"version": engine.VERSION,
        "control_hashes": {str(p): engine.sha(c.encode()) for p, c in files.items()}})
    systemctl("daemon-reload")
    systemctl("enable", "steam-large-library-fix.service")
    systemctl("enable", "--now", "steam-large-library-fix.path")
    systemctl('restart', 'steam-large-library-fix.path')
    return result


def uninstall(home):
    state = home / ".local/state/steam-large-library-fix"
    record_path = state / "installation.json"
    if not record_path.exists():
        raise ValueError("No managed installation found")
    record = json.loads(record_path.read_text())
    for name, digest in record["control_hashes"].items():
        path = Path(name)
        if not path.is_relative_to(home):
            raise ValueError("Invalid installed path")
        if path.exists():
            if (path.is_symlink() or not path.read_bytes().startswith(MARKER.encode())
                    or engine.sha(path.read_bytes()) != digest):
                raise ValueError("User-edited control file left untouched: " + name)
    systemctl("disable", "--now", "steam-large-library-fix.path", "steam-large-library-fix.service")
    for name in record["control_hashes"]:
        path = Path(name)
        if path.exists():
            path.unlink()
    systemctl("daemon-reload")
    rules = json.loads((Path(__file__).resolve().parent / "rules.json").read_text())
    result = engine.execute("restore", (home / ".local/share/Steam/steamui").resolve(), state, rules,
                            reason="uninstall")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "restored":
        raise ValueError("Later client changes were preserved; backups remain available")
    print("已卸载自动检查并恢复本工具修改的文件。恢复备份保留在用户目录。请正常重开 Steam。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--seed-backup", type=Path)
    args = parser.parse_args()
    if args.uninstall:
        uninstall(Path.home())
    else:
        install(Path.home(), args.seed_backup)


if __name__ == "__main__":
    main()
