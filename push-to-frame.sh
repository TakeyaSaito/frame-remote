#!/bin/sh
# Frame Remote: install or update the headset side from the PC, over SSH.
#
#   ./push-to-frame.sh steamos@<frame-address> [--pc http://<this-pc>:38112]
#
# Needs SSH on the Frame (Settings > System > Developer Mode). Reads the token
# from this PC's ~/.config/frame-remote/config.toml (run pc/install.sh first)
# and works out this PC's address unless --pc is given. Safe to re-run: it
# updates the program and rewrites the Frame's config.
set -eu
cd "$(dirname "$0")"

[ $# -ge 1 ] || { echo "usage: $0 user@frame-address [--pc http://this-pc:port]" >&2; exit 2; }
TARGET="$1"; shift
PC=""
[ "${1:-}" = "--pc" ] && PC="$2"

CONF="$HOME/.config/frame-remote/config.toml"
[ -f "$CONF" ] || { echo "No $CONF: run pc/install.sh first." >&2; exit 1; }
TOKEN=$(sed -n 's/^token *= *"\(.*\)".*/\1/p' "$CONF" | head -1)
PORT=$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CONF" | head -1)
PORT=${PORT:-38112}
if [ -z "$PC" ]; then
    HOST=$(echo "$TARGET" | sed 's/.*@//')
    ADDR=$(getent ahostsv4 "$HOST" | awk 'NR==1{print $1}')
    IP=$(ip -4 route get "${ADDR:-1.1.1.1}" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    [ -n "$IP" ] || { echo "Couldn't work out this PC's address; pass --pc." >&2; exit 1; }
    PC="http://$IP:$PORT"
fi

echo "Installing on $TARGET, talking to $PC"
tar -C frame -cf - install.sh frame-remote-applet.py frame-remote-applet.service |
    ssh "$TARGET" 'rm -rf ~/.cache/frame-remote-install && mkdir -p ~/.cache/frame-remote-install && tar -C ~/.cache/frame-remote-install -xf -'
# token goes over stdin, so it never shows up in a process list
printf '%s\n' "$TOKEN" | ssh "$TARGET" "cd ~/.cache/frame-remote-install && sh ./install.sh --pc '$PC' --token -"
