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

VERSION = "0.4.13"
HERE = Path(__file__).resolve().parent


class Unsupported(ValueError):
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
            raise Unsupported("Source changed during preparation; no overwrite")
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
                    raise Unsupported("Another repair is active")
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


def build(data, rules):
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


def find_client(ui, rules):
    entry = ui / "library.js"
    if not entry.is_file():
        raise Unsupported("Native Steam library entry point not found")
    loader = entry.read_text()
    matches = []
    for profile in [rules, *rules.get("alternates", [])]:
        if profile.get("loader_marker") and profile["loader_marker"] not in loader:
            continue
        if profile.get("css_sha256"):
            css = ui / "css" / "sp.css"
            if not css.is_file() or css.is_symlink() or css.stat().st_size > 16 * 1024 * 1024:
                continue
            if sha(css.read_bytes()) != profile["css_sha256"]:
                continue
        paths = [ui / profile["target"]] if "target" in profile else sorted(ui.glob("chunk~*.js"))
        for path in paths:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                continue
            if "target" not in profile and path.stem.split("~", 1)[-1] not in loader:
                continue
            data = path.read_bytes()
            try:
                _, kind = inspect_source(data.decode("utf-8"), profile)
            except (Unsupported, UnicodeError):
                continue
            matches.append((path, data, kind, profile))
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
    path, data, kind, profile = find_client(ui, rules)
    record = read_receipt(state, path.name)
    if kind == "patched" and record and record["patched_sha256"] == sha(data):
        check_backup(state, record)
        return {"status": "applied", "action": "already_applied", "file": path.name,
                "patched_sha256": sha(data)}
    original = data
    if kind == "patched":
        if seed is None:
            raise Unsupported("Existing unmanaged patch needs its original backup")
        original = seed.read_bytes()
        if build(original, profile) != data:
            raise Unsupported("Supplied backup does not reproduce this patch")
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
              "tested_client": profile["tested_client"]}
    save_json(receipt_path(state, path.name), record)
    if kind == "original":
        atomic_write(path, patched, stat.S_IMODE(path.stat().st_mode), expected=sha(data))
    return {"status": "applied", "action": "adopted" if kind == "patched" else "patched",
            "file": path.name, "patched_sha256": sha(patched)}


def restore(ui, state):
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
    path, data, kind, profile = find_client(ui, rules)
    if kind == "original":
        return {"status": "needs_apply", "file": path.name}
    record = read_receipt(state, path.name)
    if not record or sha(data) != record["patched_sha256"]:
        raise Unsupported("Unmanaged or changed patch")
    check_backup(state, record)
    return {"status": "applied", "file": path.name, "patched_sha256": sha(data),
            "tested_client": profile["tested_client"]}


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


def execute(mode, ui, state, rules, seed=None, reason="manual"):
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
        except Unsupported as error:
            result = {"status": "unsupported", "detail": str(error)}
        result.update(version=VERSION, reason=reason, checked_at=time.time())
        save_json(state / "status.json", result)
        return result


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
    if result["status"] not in {"applied", "restored"} and not args.automatic:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
