#!/usr/bin/env python3
"""Human-readable local status. Does not enable a debugger or contact a server."""
import json
from pathlib import Path
import engine

home = Path.home()
rules = json.loads((Path(__file__).resolve().parent / "rules.json").read_text())
result = engine.execute("status", (home / ".local/share/Steam/steamui").resolve(),
                        home / ".local/state/steam-large-library-fix", rules, reason="status")
if result["status"] == "applied":
    print("当前 Steam 文件中的大库修复：已安装。\n")
    print("正常重启时会检查，兼容代码被更新覆盖后会重新应用。")
    print("安装或更新后，运行中的旧界面需要正常重开 Steam 才能确认加载。")
elif result["status"] == "needs_apply":
    print("当前 Steam 文件可兼容，但修复尚未应用。正常重开 Steam 会再次检查。")
    print("若 Steam 正在更新，请先等待更新完成。")
else:
    print("当前 Steam 内部代码需要重新适配，或文件有后续改动。")
    print("工具没有强行覆盖。请保留备份，使用适配此版本的新工具。")
