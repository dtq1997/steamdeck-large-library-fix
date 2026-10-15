"""User-initiated updates from this project's GitHub releases only."""
import hashlib
import json
import re
import shlex
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'dtq1997/steamdeck-large-library-fix'
API = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=20'
DOWNLOAD = 'https://github.com/' + REPOSITORY + '/releases/download/'
LIMIT = 16 * 1024 * 1024


class UpdateError(ValueError):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise UpdateError('Unrecognized release version / 无法识别版本号')
    return tuple(map(int, value.removeprefix('v').split('.')))


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                not (host == 'github.com' or host.endswith('.githubusercontent.com'))):
            raise UpdateError('Unexpected download redirect / 下载地址异常')
        return super().redirect_request(request, response, code, message, headers, newurl)


def fetch(url, limit, *, json_response=False):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'SteamDeck-LargeLibraryFix',
        'Accept': 'application/vnd.github+json' if json_response else 'application/octet-stream',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        opener = urllib.request.build_opener(SecureRedirect())
        with opener.open(request, timeout=15) as response:
            if json_response and response.headers.get_content_type() not in {
                    'application/json', 'application/vnd.github+json'}:
                raise UpdateError('GitHub returned an unexpected response / GitHub 返回内容异常')
            if int(response.headers.get('Content-Length', '0')) > limit:
                raise UpdateError('Download exceeds size limit / 下载文件过大')
            chunks, total, deadline = [], 0, time.monotonic() + 60
            while True:
                chunk = response.read(min(65536, limit + 1 - total))
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    raise UpdateError('Download size or time limit exceeded / 下载超限或超时')
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in {403, 429}:
            raise UpdateError('GitHub rate limit or access restriction; try later / GitHub 访问受限，请稍后重试') from error
        raise UpdateError(f'GitHub HTTP {error.code}; current installation kept / 当前安装已保留') from error
    except (TimeoutError, socket.timeout) as error:
        raise UpdateError('Connection timed out; try later / 连接超时，请稍后重试') from error
    except (urllib.error.URLError, OSError) as error:
        raise UpdateError('Could not connect to GitHub; check your network / 无法连接 GitHub，请检查网络') from error


def release_assets(release):
    tag = release['tag_name']
    number = '.'.join(map(str, version(tag)))
    filename = 'SteamDeck-LargeLibraryFix-' + number + '.desktop'
    assets = {}
    for name in (filename, 'SHA256SUMS'):
        matches = [asset for asset in release.get('assets', []) if asset.get('name') == name]
        if len(matches) != 1:
            raise UpdateError('Release is missing unique installer/checksum assets / 版本安装包或校验文件不完整')
        asset = matches[0]
        expected = DOWNLOAD + tag + '/' + name
        if asset.get('browser_download_url') != expected or not 0 < asset.get('size', 0) <= LIMIT:
            raise UpdateError('Unexpected release asset / 发布文件异常')
        assets[name] = asset
    return filename, assets


def check(current):
    try:
        releases = json.loads(fetch(API, 1024 * 1024, json_response=True))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UpdateError('Invalid GitHub release list / GitHub 版本列表无效') from error
    if not isinstance(releases, list):
        raise UpdateError('Invalid GitHub release list / GitHub 版本列表无效')
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') or not release.get('published_at'):
            continue
        try:
            number = version(release.get('tag_name'))
        except UpdateError:
            continue
        if number > version(current):
            candidates.append((number, release))
    if not candidates:
        return None
    newest = max(candidates, key=lambda item: item[0])[1]
    release_assets(newest)
    return newest


def verified_download(asset, limit):
    data = fetch(asset['browser_download_url'], limit)
    digest = 'sha256:' + hashlib.sha256(data).hexdigest()
    if len(data) != asset['size'] or asset.get('digest') != digest:
        raise UpdateError('Download integrity check failed; nothing installed / 下载校验失败，未安装')
    return data


def prepare(release, directory):
    filename, assets = release_assets(release)
    checksums = verified_download(assets['SHA256SUMS'], 65536).decode('utf-8')
    matches = [line.split('  ') for line in checksums.splitlines()
               if line.endswith('  ' + filename)]
    if len(matches) != 1 or len(matches[0]) != 2 or not re.fullmatch('[0-9a-f]{64}', matches[0][0]):
        raise UpdateError('Installer checksum missing or ambiguous / 安装包校验值缺失或重复')
    data = verified_download(assets[filename], LIMIT)
    if hashlib.sha256(data).hexdigest() != matches[0][0]:
        raise UpdateError('Installer checksum mismatch; nothing installed / 安装包校验不符，未安装')
    lines = [line[5:] for line in data.decode('utf-8').splitlines() if line.startswith('Exec=')]
    argv = shlex.split(lines[0]) if len(lines) == 1 else []
    if (len(argv) not in {3, 4} or argv[:2] != ['/usr/bin/python3', '-c'] or
            (len(argv) == 4 and argv[-1] != '%k') or len(argv[2]) > 100000):
        raise UpdateError('Unsupported installer format / 不支持的安装包格式')
    path = directory / filename
    path.write_bytes(data)
    path.chmod(0o600)
    return [sys.executable, '-c', argv[2], *([str(path)] if len(argv) == 4 else [])]
