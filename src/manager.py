#!/usr/bin/env python3
"""Native desktop installation, removal and user-initiated updates."""
import argparse
import contextlib
import fcntl
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import engine
import updater

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE = HOME / '.local/state/steam-large-library-fix'
ZH = (os.environ.get('LANGUAGE') or locale.getlocale()[0] or '').startswith('zh')
TITLE = 'Steam 大库优化' if ZH else 'Steam Library Fix'


def text(english, chinese):
    return chinese if ZH else english


def dialog(kind, message, *args):
    return subprocess.run(['kdialog', '--title', TITLE, kind, message, *args],
                          text=True, capture_output=True)


def check_environment():
    if os.geteuid() == 0:
        raise ValueError(text('Open this file as your normal Deck user, without sudo.',
                              '请用 Deck 普通用户打开，不要使用 sudo。'))
    if not Path('/usr/lib/steamos/steam-launcher').is_file():
        raise ValueError(text('This installer supports native SteamOS on Steam Deck.',
                              '这个安装器仅支持 Steam Deck 的原生 SteamOS。'))
    if not shutil.which('kdialog'):
        raise ValueError('KDE kdialog is required. Open the installer in Desktop Mode.')
    if os.environ.get('XDG_CURRENT_DESKTOP', '').lower() == 'gamescope':
        raise ValueError(text('Switch to Desktop Mode, then open this file again.',
                              '请先切到桌面模式，再打开这个文件。'))


def run_installer(uninstall=False):
    command = [sys.executable, str(HERE / 'install.py')]
    if uninstall:
        command.append('--uninstall')
    STATE.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    log = STATE / 'installer.log'
    engine.atomic_write(log, (result.stdout + result.stderr).encode('utf-8'))
    if result.returncode:
        raise RuntimeError(text('The operation did not finish. See details below.',
                                '操作未完成，请查看下方详细信息。') + '\n' + result.stdout + result.stderr)
    return result


@contextlib.contextmanager
def progress(message):
    reference = None
    qdbus = shutil.which('qdbus6') or shutil.which('qdbus')
    if qdbus:
        r = dialog('--progressbar', message, '0')
        parts = r.stdout.strip().split()
        if r.returncode == 0 and len(parts) == 2 and parts[0].startswith('org.kde.kdialog'):
            reference = parts
            subprocess.run([qdbus, *reference, 'showCancelButton', 'false'],
                           capture_output=True, timeout=5)
    try:
        yield
    finally:
        if reference:
            subprocess.run([qdbus, *reference, 'close'], capture_output=True, timeout=5)


def install_now():
    with progress(text('Checking compatibility and installing…', '正在检查兼容性并安装…')):
        run_installer()
    message = text(
        'Installed. Restart Steam normally, or restart your Deck, when convenient.\n\n'
        'Steam and running games were not closed. Use “Steam Library Fix” on the desktop to check status or uninstall.',
        '已安装。方便时正常重开 Steam，或重启 Deck，即可加载补丁。\n\n'
        '安装器没有关闭 Steam 或游戏。桌面“Steam 大库优化”可查看状态和卸载。')
    status = json.loads((STATE / 'status.json').read_text())
    dialog('--msgbox', message + group_details(status))


def group_details(result):
    labels = {'wake': text('Wake data sync', '唤醒资料同步'),
              'startup': text('Startup wait', '启动等待'),
              'library': text('Library browsing', '游戏库浏览'),
              'navigation': text('Navigation and scrolling', '导航与滚动'),
              'collections': text('Collection comparison', '合集成员比较')}
    parts = []
    if result.get('active'):
        parts.append(text('Active: ', '已生效：') + ', '.join(labels.get(k, k) for k in result['active']))
    if result.get('paused'):
        parts.append(text('Paused after related Steam changes: ', '因相关 Steam 代码变化而暂停：') +
                     ', '.join(labels.get(k, k) for k in result['paused']))
    return '\n\n' + '\n'.join(parts) if parts else ''


def show_status():
    rules = json.loads((HERE / 'rules.json').read_text())
    result = engine.execute('status', (HOME / '.local/share/Steam/steamui').resolve(),
                            STATE, rules, reason='desktop-status')
    messages = {
        'applied': text('The current Steam files contain the patch.\nIf just installed, restart Steam normally to load it.',
                        '当前 Steam 文件已打上补丁。\n刚安装时，需正常重开 Steam 才会加载。'),
        'needs_apply': text('Compatible Steam files found; the patch is not currently applied.\nChoose install/repair, or restart Steam normally.',
                            '当前 Steam 文件兼容，但尚未打补丁。\n可选择安装/修复，或正常重开 Steam。'),
        'partial': text('Some optimization groups are active. Changed or unsupported groups are paused.\nRestart Steam normally after installation.',
                        '部分优化已生效，相关代码变化的项目已暂停。\n安装后需正常重开 Steam 才会加载。'),
        'busy': text('A compatibility check or repair is already running. Please check again shortly.',
                     '兼容检查或修复正在进行，请稍后再查看状态。'),
    }
    message = messages.get(result['status'], text(
        'This Steam build or another modification is not recognized. No patch was forced.\nAn updated compatible release is needed.',
        '当前 Steam 版本或其他改动尚未识别，没有强行打补丁。\n需要适配当前客户端的新版本。'))
    dialog('--msgbox', message + group_details(result) + '\n\n' + text('Tool version: ', '工具版本：') + engine.VERSION)


def uninstall_now():
    answer = dialog('--warningyesno', text(
        'Remove the patch and its automatic checks? Original backups will be kept. Steam will not be restarted.',
        '卸载补丁及自动检查？原始备份会保留，不会重开 Steam。'),
        '--yes-label', text('Uninstall', '卸载'), '--no-label', text('Cancel', '取消'))
    if answer.returncode != 0:
        return
    with progress(text('Restoring original files…', '正在恢复原始文件…')):
        run_installer(uninstall=True)
    dialog('--msgbox', text('Uninstalled. Restart Steam normally when convenient. Backups have been kept.',
                            '已卸载。方便时正常重开 Steam。原始备份已保留。'))


def update_now():
    with progress(text('Checking GitHub for updates…', '正在检查 GitHub 更新…')):
        release = updater.check(engine.VERSION)
    if release is None:
        dialog('--msgbox', text('Already up to date: ', '已是最新版本：') + engine.VERSION)
        return
    answer = dialog('--yesno', text(
        'Update to {version}? The download will be checked before installation.\n'
        'Steam will stay open and original backups will be kept.',
        '更新到 {version}？下载校验通过后才会安装。\n'
        '不会关闭 Steam，原始备份会保留。').format(version=release['tag_name']),
        '--yes-label', text('Update', '更新'), '--no-label', text('Cancel', '取消'))
    if answer.returncode:
        return
    cache = HOME / '.cache/steam-large-library-fix'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.is_symlink():
        raise ValueError('Installer cache must not be a symbolic link')
    with tempfile.TemporaryDirectory(prefix='update-', dir=cache) as directory:
        with progress(text('Downloading and verifying…', '正在下载并校验…')):
            command = updater.prepare(release, Path(directory))
        # The new installer owns its lock. Holding our menu lock here would block it.
        result = subprocess.run(command)
        if result.returncode:
            raise RuntimeError(text('Update did not finish. Check status or retry.',
                                    '更新未完成，请查看状态或重试。'))


def manage():
    choices = [
        'install', text('Install / repair this version', '安装 / 修复此版本'),
        'status', text('Check status', '查看状态'),
        'update', text('Check for updates', '检查更新'),
        'uninstall', text('Uninstall and restore', '卸载并恢复'),
        'readme', text('Read instructions', '查看说明'),
    ]
    r = dialog('--menu', text('Choose an action. Updates are checked only when requested.',
                              '选择操作。只有点击“检查更新”时才会联网查找新版。'), *choices)
    actions = {'install': install_now, 'status': show_status, 'uninstall': uninstall_now,
               'readme': lambda: dialog('--textbox', str(HERE / 'README.md'), '760', '560')}
    if r.returncode == 0 and r.stdout.strip() == 'update':
        return 'update'
    if r.returncode == 0 and r.stdout.strip() in actions:
        actions[r.stdout.strip()]()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    try:
        check_environment()
        STATE.mkdir(parents=True, exist_ok=True)
        action = None
        with (STATE / 'installer.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                dialog('--msgbox', text('Another installer window is already open.', '另一个安装窗口已打开。'))
                return 1
            if args.install:
                install_now()
            else:
                action = manage()
        if action == 'update':
            update_now()
    except Exception as error:
        details = traceback.format_exc()
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            engine.atomic_write(STATE / 'installer-error.log', details.encode('utf-8'))
        except OSError:
            pass
        if shutil.which('kdialog'):
            dialog('--detailederror', text('Could not complete this operation.\nNo Steam restart was requested.',
                                          '未能完成操作。\n安装器没有要求重开 Steam。'), str(error)[-5000:])
        print(details, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
