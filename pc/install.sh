#!/bin/sh
# Frame Remote: install the PC side (run as your normal user, not root).
#
#   ./install.sh            install / update, keep an existing config
#   ./install.sh --uninstall
#
# Installs to ~/.local/share/frame-remote/, config in ~/.config/frame-remote/,
# and a systemd user service "frame-remote" that starts with your session.
set -eu
cd "$(dirname "$0")"

DEST="$HOME/.local/share/frame-remote"
CONF_DIR="$HOME/.config/frame-remote"
CONF="$CONF_DIR/config.toml"
UNIT_DIR="$HOME/.config/systemd/user"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now frame-remote.service 2>/dev/null || true
    rm -f "$UNIT_DIR/frame-remote.service" "$DEST/frame-remote-server" "$DEST/frame-remote-steam"
    systemctl --user daemon-reload
    echo "Removed. Your config is still in $CONF_DIR (delete it by hand if you want)."
    exit 0
fi

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root." >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    echo "Needs Python 3.11 or newer." >&2; exit 1; }
command -v systemd-run >/dev/null || { echo "Needs systemd." >&2; exit 1; }

mkdir -p "$DEST" "$CONF_DIR" "$UNIT_DIR"
chmod 700 "$CONF_DIR"
install -m 755 frame-remote-server frame-remote-steam "$DEST/"
install -m 644 config.example.toml "$DEST/"

if [ ! -f "$CONF" ]; then
    token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    sed "s|^token = \"CHANGE-ME\"|token = \"$token\"|" config.example.toml > "$CONF"
    chmod 600 "$CONF"
    echo "Created $CONF with a new token."
else
    echo "Kept your existing $CONF (the current example is in $DEST/config.example.toml)."
fi

sed "s|@DEST@|$DEST|" frame-remote.service > "$UNIT_DIR/frame-remote.service"
systemctl --user daemon-reload
systemctl --user enable frame-remote.service >/dev/null 2>&1
systemctl --user restart frame-remote.service
sleep 1
if ! systemctl --user is-active -q frame-remote.service; then
    echo "The service didn't start:" >&2
    journalctl --user -u frame-remote -n 10 --no-pager -o cat >&2
    exit 1
fi

port=$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CONF" | head -1)
port=${port:-38112}
token=$(sed -n 's/^token *= *"\(.*\)".*/\1/p' "$CONF" | head -1)
ip=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')

cat <<EOF

Frame Remote server is running on port $port.

1. Let the Frame through your firewall, if you run one (use the Frame's address
   instead of the network to be stricter):
     ufw:        sudo ufw allow from 192.168.0.0/16 to any port $port proto tcp
     firewalld:  sudo firewall-cmd --permanent --add-port=$port/tcp && sudo firewall-cmd --reload

2. Install the headset side. Either from this PC over SSH (Developer Mode on the Frame):
     ../push-to-frame.sh steamos@<frame-address>
   or on the Frame itself (Desktop Mode terminal), from the frame/ folder:
     ./install.sh --pc http://${ip:-<this-pc-address>}:$port --token $token

Edit the buttons in $CONF, then: systemctl --user restart frame-remote
EOF
