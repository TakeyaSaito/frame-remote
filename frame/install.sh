#!/bin/sh
# Frame Remote: install the headset side. Run ON the Steam Frame (over SSH, or in
# a Desktop Mode terminal), as the normal user, from this folder.
#
#   ./install.sh --pc http://192.168.1.10:38112 --token <token from the PC>
#                  (--token - reads it from stdin; an existing frame.toml keeps
#                   everything except the pc/token lines)
#   ./install.sh               update the program, keep the existing config
#   ./install.sh --uninstall
#
# Everything goes under your home folder (survives SteamOS updates, no root):
#   ~/.local/share/frame-remote/   program + Python venv
#   ~/.config/frame-remote/        frame.toml (PC address + token)
#   ~/.config/systemd/user/frame-remote-applet.service
set -eu
cd "$(dirname "$0")"

DEST="$HOME/.local/share/frame-remote"
CONF_DIR="$HOME/.config/frame-remote"
CONF="$CONF_DIR/frame.toml"
UNIT_DIR="$HOME/.config/systemd/user"
PC="" TOKEN=""

while [ $# -gt 0 ]; do
    case "$1" in
        --pc) PC="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --uninstall)
            systemctl --user disable --now frame-remote-applet.service 2>/dev/null || true
            rm -rf "$DEST" "$UNIT_DIR/frame-remote-applet.service"
            systemctl --user daemon-reload
            echo "Removed. Config left in $CONF_DIR."
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$TOKEN" = "-" ] && read -r TOKEN    # --token - : read it from stdin
if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user (steamos), not root." >&2
    exit 1
fi
if [ ! -f "$CONF" ] && { [ -z "$PC" ] || [ -z "$TOKEN" ]; }; then
    echo "First install needs --pc http://<pc-address>:38112 --token <token>" >&2
    echo "(the PC's install.sh prints both)." >&2
    exit 2
fi

mkdir -p "$DEST" "$CONF_DIR" "$UNIT_DIR"
chmod 700 "$CONF_DIR"

if [ -n "$PC" ]; then
    case "$PC" in http://*|https://*) ;; *) PC="http://$PC" ;; esac
    case "$PC" in *://*:[0-9]*) ;; *) PC="$PC:38112" ;; esac
fi
if [ -n "$PC" ] && [ -f "$CONF" ]; then
    # keep the rest of the file (local buttons, toast settings): only swap these two lines
    tmp=$(mktemp "$CONF_DIR/.frame.toml.XXXXXX")
    awk -v pc="$PC" -v tok="$TOKEN" '
        /^pc *=/    { print "pc = \"" pc "\""; next }
        /^token *=/ { if (tok != "") { print "token = \"" tok "\""; next } }
        { print }' "$CONF" > "$tmp"
    chmod 600 "$tmp" && mv "$tmp" "$CONF"
    echo "Updated pc/token in $CONF (the rest of it kept)"
elif [ -n "$PC" ]; then
    umask 077
    cat > "$CONF" <<EOF
# Frame Remote: headset side. The PC's buttons are configured on the PC.
pc = "$PC"
token = "$TOKEN"

# Name of the dashboard icon and the panel title.
# title = "PC Remote"

# Local buttons run here on the headset, so they work while the PC is off.
# Same fields as the PC's buttons (id, title, subtitle, row, confirm) plus:
#   wake = "aa:bb:cc:dd:ee:ff"    Wake-on-LAN: the PC's MAC address
#   broadcast = "192.168.1.255"   optional, your network's broadcast address
#   command = ["sh", "-c", "..."] or a command run on the headset instead
#   show = "always" | "offline" | "online"   when the PC is (un)reachable
# A row with the same name as a PC row shares it.
#
# [[button]]
# id = "wake"
# title = "Wake PC"
# subtitle = "Wake-on-LAN"
# row = "Power"
# show = "offline"
# wake = "aa:bb:cc:dd:ee:ff"

# The pop-up shown in front of your eyes when a command starts and finishes.
[toast]
enabled = true
seconds = 4
distance = 1.2    # metres ahead
height = -0.22    # metres up from eye level (negative = lower)
EOF
    umask 022
    echo "Wrote $CONF"
fi

if [ ! -x "$DEST/venv/bin/python" ]; then
    echo "Creating the Python environment (needs internet, about a minute)..."
    python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/pip" install -q --disable-pip-version-check --upgrade openvr pillow PyOpenGL

install -m 755 frame-remote-applet.py "$DEST/"
install -m 644 frame-remote-applet.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable frame-remote-applet.service >/dev/null 2>&1
systemctl --user restart frame-remote-applet.service

# quick check that the PC answers
PC_NOW=$(sed -n 's/^pc *= *"\(.*\)".*/\1/p' "$CONF")
TOKEN_NOW=$(sed -n 's/^token *= *"\(.*\)".*/\1/p' "$CONF")
code=$(curl -s -o /dev/null -m 4 -w '%{http_code}' -H "X-Token: $TOKEN_NOW" "$PC_NOW/status" || true)
case "$code" in
    200) echo "PC at $PC_NOW answers. ✓" ;;
    403) echo "PC at $PC_NOW refused: wrong token, or this headset's address isn't in [server].allowed." ;;
    *)   echo "Couldn't reach the PC at $PC_NOW (firewall? server running?)." ;;
esac
sleep 3
if systemctl --user is-active -q frame-remote-applet.service; then
    echo "Installed. Open the SteamVR dashboard: the orange icon in the bottom bar is the panel."
else
    echo "The applet didn't start:" >&2
    journalctl --user -u frame-remote-applet -n 15 --no-pager -o cat >&2
    exit 1
fi
