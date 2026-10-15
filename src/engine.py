#!/usr/bin/env python3
"""Steam library fix: local, signature-gated, atomic, reversible. [Codex]"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

import compat

VERSION = "0.4.14"
HERE = Path(__file__).resolve().parent


class Unsupported(ValueError):
    pass


class Busy(Unsupported):
    pass


class Retry(Unsupported):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o600, expected=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".llf-", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, mode)
        if expected is not None and (path.is_symlink() or sha(path.read_bytes()) != expected):
            raise Retry("Source changed during preparation; no overwrite")
        os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staging.unlink(missing_ok=True)


def save_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


@contextlib.contextmanager
def locked(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "manager.lock").open("a") as stream:
        deadline = time.monotonic() + 4
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Busy("Another repair is active")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def fragments(source, rule):
    start = 0
    while True:
        index = source.find(rule["start"], start)
        if index < 0:
            return
        end = source.find(rule["end"], index + len(rule["start"]),
                          index + 12000 + len(rule["end"]))
        if 0 < end - index < 12000:
            yield index, end, source[index:end]
        start = index + len(rule["start"])


def method_match(source, rule):
    accepted = []
    for start, end, text in fragments(source, rule):
        digest = sha(text.encode())
        for kind in ("original", "patched"):
            if digest == rule[kind + "_sha256"]:
                accepted.append((start, end, kind))
    if len(accepted) != 1:
        raise Unsupported("Unknown or ambiguous method implementation")
    return accepted[0]


def check_contracts(source, rules):
    for rule in rules["contracts"]:
        if "literal" in rule:
            valid = rule["literal"] in source
        else:
            valid = any(sha(text.encode()) == rule["sha256"]
                        for _, _, text in fragments(source, rule))
        if not valid:
            raise Unsupported("Filter dependency changed; no patch")


def inspect_source(source, rules):
    if rules.get("bundle_format") == "sp":
        opening = "(()=>{"
        closing = r"\}\]\);\}\)\(\);\s*//# sourceMappingURL=[^\r\n]+/sp\.js\.map\s*$"
    else:
        opening = "(self.webpackChunksteamui=self.webpackChunksteamui||[]).push("
        closing = r"\}\}\]\);//# sourceMappingURL=[^\r\n]+\.js\.map\s*$"
    if opening not in source[:256] or not re.search(closing, source):
        raise Unsupported("Incomplete or unknown client bundle; no patch")
    check_contracts(source, rules)
    found = [method_match(source, rule) for rule in rules["methods"]]
    kinds = {item[2] for item in found}
    if len(kinds) != 1:
        raise Unsupported("Partly modified source; no patch")
    return found, kinds.pop()


def build_legacy(data, rules):
    source = data.decode("utf-8")
    found, kind = inspect_source(source, rules)
    if kind == "patched":
        return data
    changes = sorted(zip(found, rules["methods"]), key=lambda x: x[0][0], reverse=True)
    for (start, end, _), rule in changes:
        source = source[:start] + rule["replacement"] + source[end:]
    result = source.encode()
    spare = len(data) - len(result)
    if spare < 0:
        raise Unsupported("Cannot preserve client file size")
    if spare:
        result += b"\n" + b" " * (spare - 1)
    return result


def metadata(profile):
    value = json.loads((HERE / 'compatibility.json').read_text())
    if value['version'] != VERSION:
        raise Unsupported('Compatibility metadata version mismatch')
    return value['profiles'][profile['tested_client']]


def compatible_plan(data, profile, css=None):
    source = data.decode('utf-8')
    # Retain full-bundle completeness checks before any tolerant matching.
    if profile.get('bundle_format') == 'sp':
        opening, closing = '(()=>{', r'\}\]\);\}\)\(\);\s*//# sourceMappingURL=[^\r\n]+/sp\.js\.map\s*$'
    else:
        opening = '(self.webpackChunksteamui=self.webpackChunksteamui||[]).push('
        closing = r'\}\}\]\);//# sourceMappingURL=[^\r\n]+\.js\.map\s*$'
    # Permit trailing comments while keeping the actual wrapper and source-map boundary.
    tail = r'(?:\s+|//[^\r\n]*(?:\r?\n|$)|/\*[\s\S]*?\*/)*\Z'
    closing = closing.removesuffix(r'\s*$') + tail
    if opening not in source[:256] or not re.search(closing, source):
        raise Unsupported('Incomplete or unknown client bundle; no patch')
    try:
        result = compat.plan(source, profile, metadata(profile), css)
    except compat.Incompatible as error:
        raise Unsupported(str(error)) from error
    return result


def build(data, rules):
    plan = rules.get('_plan')
    if plan is None:
        # Direct developer calls retain the established known-client behavior.
        return build_legacy(data, rules)
    source = data.decode('utf-8')
    changes = list(plan['changes'])
    delta = sum(len(replacement.encode()) - len(source[start:end].encode()) for start, end, replacement in changes)
    if delta > 0:
        for start, end, replacement in plan.get('spellings', []):
            if any(start < b and a < end for a, b, _ in changes):
                continue
            changes.append((start, end, replacement))
            delta += len(replacement.encode()) - len(source[start:end].encode())
            if delta <= 0:
                break
    previous = len(source) + 1
    for start, end, replacement in sorted(changes, reverse=True):
        if not (0 <= start < end <= len(source)) or end > previous:
            raise Unsupported('Overlapping or invalid patch ranges')
        source = source[:start] + replacement + source[end:]
        previous = start
    output = source.encode('utf-8')
    spare = len(data) - len(output)
    if spare < 0:
        raise Unsupported('Cannot preserve client file size')
    return output + (b'\n' + b' ' * (spare - 1) if spare else b'')


def find_client(ui, rules, state=None):
    entry = ui / "library.js"
    if not entry.is_file():
        raise Unsupported("Native Steam library entry point not found")
    loader = entry.read_text()
    matches = []
    for profile in [rules, *rules.get("alternates", [])]:
        if profile.get("loader_marker") and profile["loader_marker"] not in loader:
            continue
        css_data = None
        if profile.get("css_sha256"):
            css = ui / "css" / "sp.css"
            if css.is_file() and not css.is_symlink() and css.stat().st_size <= 16 * 1024 * 1024:
                css_data = css.read_bytes()
        paths = [ui / profile["target"]] if "target" in profile else sorted(ui.glob("chunk~*.js"))
        for path in paths:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                continue
            if "target" not in profile and path.stem.split("~", 1)[-1] not in loader:
                continue
            data = path.read_bytes()
            base = data
            record = read_receipt(state, path.name) if state is not None else None
            if record and record['patched_sha256'] == sha(data):
                base = check_backup(state, record).read_bytes()
            try:
                plan = compatible_plan(base, profile, css_data)
            except (Unsupported, UnicodeError):
                continue
            selected = {**profile, '_plan': plan, '_base': base, '_css': css_data}
            kind = 'managed' if base != data else plan['kind']
            matches.append((path, data, kind, selected))
    if len(matches) != 1:
        raise Unsupported("Current active client code is unknown or ambiguous")
    return matches[0]


def receipt_path(state, name):
    if Path(name).name != name or not (name == "sp.js" or name.startswith("chunk~") and name.endswith(".js")):
        raise Unsupported("Invalid receipt target")
    return state / "receipts" / (name + ".json")


def read_receipt(state, name):
    path = receipt_path(state, name)
    return json.loads(path.read_text()) if path.exists() else None


def check_backup(state, record):
    digest = record["original_sha256"]
    if len(digest) != 64 or any(x not in "0123456789abcdef" for x in digest):
        raise Unsupported("Invalid backup identifier")
    backup = state / "backups" / (digest + ".js")
    if sha(backup.read_bytes()) != digest:
        raise Unsupported("Backup integrity check failed")
    return backup


def install_patch(ui, state, rules, seed=None):
    recover_pending(ui, state)
    path, data, kind, profile = find_client(ui, rules, state)
    record = read_receipt(state, path.name)
    original = profile['_base']
    if kind in {'patched', 'mixed'}:
        if seed is None:
            raise Unsupported("Existing unmanaged patch needs its original backup")
        original = seed.read_bytes()
        check = compatible_plan(original, profile, profile.get('_css'))
        if build(original, {**profile, '_plan': check}) != data:
            raise Unsupported("Supplied backup does not reproduce this patch")
        profile = {**profile, '_plan': check, '_base': original}
    patched = build(original, profile)
    digest = sha(original)
    backup = state / "backups" / (digest + ".js")
    if backup.exists():
        if sha(backup.read_bytes()) != digest:
            raise Unsupported("Existing backup is damaged")
    else:
        atomic_write(backup, original)
    record = {"file": path.name, "original_sha256": digest,
              "patched_sha256": sha(patched), "bytes": len(original), "version": VERSION,
              "tested_client": profile["tested_client"], 'active': profile['_plan']['active'],
              'paused': profile['_plan']['paused']}
    pending = state / 'pending.json'
    if data != patched:
        if 'navigation' in profile['_plan']['active']:
            css = ui / 'css/sp.css'
            if not css.is_file() or css.is_symlink() or sha(css.read_bytes()) != sha(profile['_css']):
                raise Retry('Layout changed during preparation; retry after the update')
        save_json(pending, {'record': record, 'before': sha(data)})
        atomic_write(path, patched, stat.S_IMODE(path.stat().st_mode), expected=sha(data))
    save_json(receipt_path(state, path.name), record)
    pending.unlink(missing_ok=True)
    return patch_result(record, 'already_applied' if data == patched else 'patched')


def patch_result(record, action):
    return {'status': 'partial' if record.get('paused') else 'applied', 'action': action,
            'file': record['file'], 'patched_sha256': record['patched_sha256'],
            'active': record.get('active', []), 'paused': record.get('paused', {}),
            'tested_client': record['tested_client']}


def recover_pending(ui, state):
    pending = state / 'pending.json'
    if not pending.exists():
        return
    value = json.loads(pending.read_text())
    record = value['record']
    target = ui / record['file']
    receipt_path(state, record['file'])
    if not target.is_file() or target.is_symlink():
        raise Unsupported('Pending transaction target unavailable')
    current = sha(target.read_bytes())
    if current == record['patched_sha256']:
        check_backup(state, record)
        save_json(receipt_path(state, record['file']), record)
    elif current != value['before']:
        archive = state / 'interrupted' / (record['patched_sha256'] + '-' + current + '.json')
        save_json(archive, value)
    pending.unlink()


def restore(ui, state):
    recover_pending(ui, state)
    results = []
    for item in sorted((state / "receipts").glob("*.json")):
        record = json.loads(item.read_text())
        receipt_path(state, record["file"])
        path = ui / record["file"]
        if not path.exists():
            continue
        current = sha(path.read_bytes())
        if current == record["original_sha256"]:
            continue
        if current != record["patched_sha256"] or path.is_symlink():
            results.append({"file": path.name, "status": "conflict_left_untouched"})
            continue
        backup = check_backup(state, record)
        atomic_write(path, backup.read_bytes(), stat.S_IMODE(path.stat().st_mode), expected=current)
        results.append({"file": path.name, "status": "restored"})
    conflict = any(x["status"] != "restored" for x in results)
    return {"status": "conflict" if conflict else "restored", "files": results}


def inspect_status(ui, state, rules):
    recover_pending(ui, state)
    path, data, kind, profile = find_client(ui, rules, state)
    expected = build(profile['_base'], profile)
    if data != expected:
        return {"status": "needs_apply", "file": path.name}
    record = read_receipt(state, path.name)
    if not record or sha(data) != record["patched_sha256"]:
        raise Unsupported("Unmanaged or changed patch")
    check_backup(state, record)
    return patch_result(record, 'checked')


def updater_busy(proc=Path("/proc")):
    names = set()
    if proc.is_dir():
        for directory in proc.iterdir():
            if directory.name.isdigit():
                try:
                    names.add((directory / "comm").read_text().strip())
                except OSError:
                    continue
    return "steam" in names and "steamwebhelper" not in names


def execute_locked(mode, ui, state, rules, seed, reason):
    with locked(state):
        try:
            if mode == "apply":
                if updater_busy():
                    result = {"status": "deferred", "detail": "Steam bootstrap/update is active; left untouched"}
                else:
                    result = install_patch(ui, state, rules, seed)
            elif mode == "restore":
                result = restore(ui, state)
            else:
                result = inspect_status(ui, state, rules)
        except Retry as error:
            result = {'status': 'deferred', 'detail': str(error)}
        except Unsupported as error:
            result = {"status": "unsupported", "detail": str(error)}
        result.update(version=VERSION, reason=reason, checked_at=time.time())
        save_json(state / "status.json", result)
        return result


def execute(mode, ui, state, rules, seed=None, reason='manual'):
    try:
        return execute_locked(mode, ui, state, rules, seed, reason)
    except Busy as error:
        return {'status': 'busy', 'detail': str(error), 'version': VERSION,
                'reason': reason, 'checked_at': time.time()}


def main():
    parser = argparse.ArgumentParser(description="Steam Deck large-library fix")
    parser.add_argument("mode", choices=["apply", "restore", "status"])
    parser.add_argument("--ui", type=Path, default=Path.home() / ".local/share/Steam/steamui")
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/steam-large-library-fix")
    parser.add_argument("--seed-backup", type=Path)
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--automatic", action="store_true")
    args = parser.parse_args()
    rules = json.loads((HERE / "rules.json").read_text())
    result = execute(args.mode, args.ui.resolve(), args.state, rules,
                     args.seed_backup, args.reason)
    print(json.dumps(result, ensure_ascii=False))
    if result['status'] in {'deferred', 'busy'} and args.automatic:
        raise SystemExit(75)
    if result["status"] not in {"applied", "partial", "restored"} and not args.automatic:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
