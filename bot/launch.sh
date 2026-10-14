#!/bin/bash
# Builds the bridge and (re)launches Mini Motorways with it injected.
# Quits a running copy of the game first.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
app="/Applications/Mini Motorways.app"
port="${MMBRIDGE_PORT:-47811}"

make -s -C "$here/bridge"

# dyld maps the library before the sandbox starts, but keep it out of TCC-protected folders
# such as ~/Desktop so the load never depends on folder permissions.
libdir="$HOME/Library/Application Support/mmbot"
mkdir -p "$libdir"
cp "$here/bridge/build/libmmbridge.dylib" "$libdir/"

if pgrep -x "Mini Motorways" >/dev/null; then
    osascript -e 'quit app "Mini Motorways"' || true
    while pgrep -x "Mini Motorways" >/dev/null; do sleep 0.5; done
fi

token="$(openssl rand -hex 16)"
(umask 077 && printf '%s\n' "$token" >"$here/.mmbridge_token")

# Exec the game directly: SIP strips DYLD_* variables from anything run through a protected
# system binary such as /usr/bin/nohup or /usr/bin/env.
DYLD_INSERT_LIBRARIES="$libdir/libmmbridge.dylib" MMBRIDGE_TOKEN="$token" MMBRIDGE_PORT="$port" \
    "$app/Contents/MacOS/Mini Motorways" </dev/null >/dev/null 2>&1 &
pid=$!
disown
echo "Mini Motorways launched (pid $pid); the bridge will connect to 127.0.0.1:$port"
