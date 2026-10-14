#!/bin/bash
# Run from the extracted directory; no administrator password is required.
set -u
cd -- "$(dirname -- "$0")" || exit 1
python3 install.py "$@"
result=$?
if [ -t 0 ]; then
    read -r -p '按回车关闭。' _reply
fi
exit "$result"
