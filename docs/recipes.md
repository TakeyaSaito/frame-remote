# Recipes

Step-by-step setups for the things Frame Remote is most used for. Each one is independent; pick the ones you want. The full example at the end puts them all together.

Addresses and names in the examples are placeholders: replace `192.168.1.10` (the PC), `192.168.1.50` (the Frame) and `aa:bb:cc:dd:ee:ff` (the PC's MAC address) with your own.

Two config files are involved:
- **`~/.config/frame-remote/config.toml` on the PC**: buttons that run on the PC. Apply changes with `systemctl --user restart frame-remote`.
- **`~/.config/frame-remote/frame.toml` on the Frame**: local buttons that run on the headset. Apply changes with `systemctl --user restart frame-remote-applet` on the Frame (or `ssh steamos@192.168.1.50 systemctl --user restart frame-remote-applet`).

Buttons that share a `row` name sit side by side under that heading, even when one is a PC button and the other a local one.

---

## 1. Wake the PC from the headset

A local button on the Frame sends a Wake-on-LAN packet, then waits until the PC's Frame Remote server answers and tells you "PC is up (30 s)".

**On the PC, once:**

1. Use a **wired** connection (Wake-on-LAN over Wi-Fi rarely works).
2. In the BIOS/UEFI, turn **Wake on LAN** (sometimes "Power On By PCI-E") **on**, and **ErP / EuP / Deep Sleep off** (those cut power to the network card when the PC is off).
3. Find the wired interface and its MAC address:
   ```sh
   ip -br link      # e.g.  enp5s0   UP   aa:bb:cc:dd:ee:ff
   ```
4. Check Wake-on-LAN is enabled on the card (`g` = magic packet):
   ```sh
   sudo ethtool enp5s0 | grep Wake-on
   ```
   If it says `d`, enable it permanently through NetworkManager (find the connection name with `nmcli connection show`):
   ```sh
   sudo nmcli connection modify "Wired connection 1" 802-3-ethernet.wake-on-lan magic
   ```
5. Find your network's broadcast address (optional but more reliable). `ip -br addr show enp5s0` shows something like `192.168.1.10/24`, and for a `/24` network the broadcast is `192.168.1.255`.

If a phone app can already wake your PC, steps 1 to 4 are done.

**On the Frame**, add to `frame.toml`:

```toml
[[button]]
id = "wake"
title = "Wake PC"
subtitle = "Wake-on-LAN"
row = "Power"
show = "offline"                 # only appears while the PC can't be reached
wake = "aa:bb:cc:dd:ee:ff"
broadcast = "192.168.1.255"
```

With `show = "offline"`, the panel shows just this button while the PC is off, and the normal buttons once it's up. Use `show = "always"` if you'd rather always see it. If waking takes longer than 150 seconds (slow boot, disk check), raise `wake_timeout = 240` at the top of `frame.toml`.

---

## 2. Shut down the PC (and restart or suspend)

On the PC, add to `config.toml`:

```toml
[[button]]
id = "shutdown"
title = "Shut Down PC"
subtitle = "Powers off in 5 s"
row = "Power"
confirm = true
command = ["sh", "-c", "systemd-run --user --quiet --unit=frame-remote-poweroff sh -c 'sleep 5 && systemctl poweroff' && echo 'Shutting down in 5 seconds.'"]
```

Why the 5-second delay: the button's command has to finish and report back before the PC goes away, or the panel would only ever see "PC not reachable". `systemd-run` starts the delayed shutdown as its own little job, so the button returns straight away. (Don't use `systemd-run --on-active=5`: user timers default to one minute of accuracy, so "5 seconds" can turn into a minute.)

`confirm = true` means it needs a second tap within 4 seconds, so a stray click can't turn your PC off.

Put it in the same `row` as the Wake button from recipe 1: the row then holds **Shut Down** while the PC is on and **Wake** while it's off.

**Restart and suspend** work the same way; swap `systemctl poweroff` for `systemctl reboot` or `systemctl suspend`:

```toml
[[button]]
id = "restart"
title = "Restart PC"
row = "Power"
confirm = true
command = ["sh", "-c", "systemd-run --user --quiet --unit=frame-remote-reboot sh -c 'sleep 5 && systemctl reboot' && echo 'Restarting in 5 seconds.'"]
```

To check that your desktop allows this without a password prompt:

```sh
busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanPowerOff
```

`s "yes"` means it works. `"challenge"` means a password would be asked for; on most desktops that only happens when other users are logged in.

---

## 3. The VR session: Start SteamVR, Close Game, Close SteamVR

These are the three default buttons, and the example `config.toml` already has them. The flow when streaming PC VR games to the Frame from Linux:

1. **Start SteamVR** before launching the game. On a Linux host, Proton VR games don't start SteamVR by themselves; they just run without VR. Native VR games usually do start it, but starting it first doesn't hurt. If Steam itself has crashed or was closed, this button starts Steam first.
2. Launch the game from the Frame as usual.
3. When you're done, quit the game. Quitting a VR game on the headset can leave the game, SteamVR, or both running on the PC, so:
   - **Close Game** closes every running Steam game and leaves SteamVR up, ready for the next game.
   - **Close SteamVR** closes SteamVR, and any game still using VR (it would start SteamVR again otherwise).

Both close buttons need a second tap. The panel's status line shows which games are running, so you can see what will be closed.

```toml
[[button]]
id = "steamvr-start"
title = "Start SteamVR"
subtitle = "Before a Proton VR game"
row = "VR"
command = ["frame-remote-steam", "start-vr"]

[[button]]
id = "close-games"
title = "Close Game"
subtitle = "SteamVR keeps running"
row = "VR"
confirm = true
command = ["frame-remote-steam", "close-games"]

[[button]]
id = "steamvr-close"
title = "Close SteamVR"
subtitle = "And any VR game"
row = "VR"
confirm = true
command = ["frame-remote-steam", "close-vr"]
```

There's also `frame-remote-steam close-all` (every game and SteamVR), if you want a single "close everything" button.

---

## 4. A streaming screen layout ("Frame mode")

When you stream 2D games or the desktop to the Frame, it helps to switch the PC to a layout made for it: desk monitors off, and one screen at the resolution you want in the headset. Steam Remote Play streams the **primary** display, so whatever screen you make primary is what the headset gets.

Make two small scripts that switch the layout, plus a state file so the panel can show a status light. This example uses KDE Plasma's `kscreen-doctor`. Run `kscreen-doctor -o` to see your output names and modes.

`~/.local/bin/stream-layout`:

```sh
#!/bin/sh
# stream-layout on|off: switch the PC between the desk layout and a streaming layout.
STATE="$HOME/.local/state/stream-layout-on"
case "$1" in
    on)
        # Example: turn the desk monitors off and use the TV (HDMI-A-1) at 2560x1440@120 as the only screen.
        kscreen-doctor output.HDMI-A-1.enable output.HDMI-A-1.mode.2560x1440@120 \
                       output.HDMI-A-1.primary output.DP-1.disable output.DP-2.disable &&
        mkdir -p "$(dirname "$STATE")" && touch "$STATE" && echo "Streaming layout on." ;;
    off)
        kscreen-doctor output.DP-1.enable output.DP-2.enable output.DP-1.primary \
                       output.HDMI-A-1.disable &&
        rm -f "$STATE" && echo "Desk layout back." ;;
    *) echo "usage: stream-layout on|off" >&2; exit 2 ;;
esac
```

```sh
chmod +x ~/.local/bin/stream-layout
```

Then in `config.toml`:

```toml
[[button]]
id = "layout-on"
title = "Frame Mode On"
subtitle = "Desk screens off"
row = "Screen"
command = ["~/.local/bin/stream-layout", "on"]

[[button]]
id = "layout-off"
title = "Frame Mode Off"
subtitle = "Desk monitors back"
row = "Screen"
command = ["~/.local/bin/stream-layout", "off"]

[[indicator]]
label = "Frame mode"
check = ["test", "-e", "~/.local/state/stream-layout-on"]
```

Other desktops: on X11 sessions, `xrandr --output HDMI-1 --mode 2560x1440 --rate 120 --primary --output DP-1 --off` does the same; recent GNOME versions have `gdctl` for this.

**No spare screen to switch to?** Some people stream from a **virtual display** instead: an HDMI/DisplayPort dummy plug (a cheap adapter that pretends to be a monitor), or a software virtual output such as EVDI. Then "on" enables the virtual screen at the headset's resolution and turns the real ones off, so nothing lights up at your desk while you play. The script stays the same; only the output names change.

---

## 5. The full PC desktop in the headset (Moonlight + Sunshine)

Steam on the Frame streams games, not your whole desktop. For the full desktop, use **Sunshine** on the PC and **Moonlight** on the Frame, both free and open source. With the layout script from recipe 4 as Sunshine's prep command, starting the stream switches the PC into Frame mode and ending it switches back.

**On the PC:**

1. Install Sunshine ([sunshine docs](https://docs.lizardbyte.dev/projects/sunshine/latest/)) and open its web UI at `https://localhost:47990`. Set a username and password the first time.
2. Allow Moonlight through your firewall from the Frame: TCP 47984, 47989 and 48010, and UDP 47998 to 48000 (Sunshine's docs have the full list). The web UI port, 47990, only needs to be reachable from the PC itself, so leave it closed to the network.
3. In the web UI's **Applications** tab, add an app:
   - **Name:** `Frame Desktop`
   - **Command preparations:** *Do* `/home/YOU/.local/bin/stream-layout on`, *Undo* `/home/YOU/.local/bin/stream-layout off` (full paths: don't rely on `~` being expanded here)
   - Leave *Command* empty: an app with no command just streams the desktop.

**On the Frame** (over SSH, or in a Desktop Mode terminal):

1. Install Moonlight from Flathub as a user app (no root, survives SteamOS updates):
   ```sh
   flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
   flatpak install --user -y flathub com.moonlight_stream.Moonlight
   ```
2. Pair it with Sunshine. Start pairing with a PIN of your choice (Moonlight waits up to a few minutes):
   ```sh
   QT_QPA_PLATFORM=offscreen flatpak run com.moonlight_stream.Moonlight pair 192.168.1.10 --pin 1234
   ```
   then on the PC, open `https://localhost:47990/pin` and enter the same PIN. Check that it worked by listing the PC's apps from the Frame:
   ```sh
   QT_QPA_PLATFORM=offscreen flatpak run com.moonlight_stream.Moonlight list 192.168.1.10
   ```
   ("Frame Desktop" must be in the list; listing also refreshes Moonlight's app cache, which the next step needs.)
3. Add it to the Frame's library as a non-Steam game. These steps follow the Steam Deck; menu names on the Frame may differ. In Desktop Mode, open Steam, choose **Games → Add a Non-Steam Game to My Library**, and pick **Moonlight**. Then open the new entry's **Properties** and set the launch options to:
   ```
   run com.moonlight_stream.Moonlight stream 192.168.1.10 "Frame Desktop" --resolution 2560x1440 --fps 120 --absolute-mouse --display-mode fullscreen --quit-after
   ```
   (Rename it to "PC Desktop" if you like. Keep a second, plain Moonlight entry for browsing your other Sunshine apps and changing settings.)

Launching it from the library then switches the PC into the streaming layout and shows the full desktop in the headset. Quitting switches the desk layout back: `--quit-after` ends the Sunshine app, which runs its *Undo*. `--absolute-mouse` makes the pointer behave like a remote desktop instead of a game.

**Tip:** if you also use the Frame Mode buttons from recipe 4, have the *Undo* only switch back when the stream was what switched it on, so ending a stream doesn't undo a layout you set yourself. For example, wrap the script so `on` leaves a marker file when the layout was off before, and `off` only acts when that marker is there.

---

## 6. Putting it together

This is a complete setup with everything above.

**PC: `~/.config/frame-remote/config.toml`**

```toml
[server]
token = "…"                        # made by install.sh
name = "Gaming PC"
allowed = ["192.168.1.50"]         # only the Frame
show_games = true

[[button]]
id = "layout-on"
title = "Frame Mode On"
subtitle = "Desk screens off"
row = "Screen"
command = ["~/.local/bin/stream-layout", "on"]

[[button]]
id = "layout-off"
title = "Frame Mode Off"
subtitle = "Desk monitors back"
row = "Screen"
command = ["~/.local/bin/stream-layout", "off"]

[[button]]
id = "steamvr-start"
title = "Start SteamVR"
subtitle = "Before a Proton VR game"
row = "VR"
command = ["frame-remote-steam", "start-vr"]

[[button]]
id = "close-games"
title = "Close Game"
subtitle = "SteamVR keeps running"
row = "VR"
confirm = true
command = ["frame-remote-steam", "close-games"]

[[button]]
id = "steamvr-close"
title = "Close SteamVR"
subtitle = "And any VR game"
row = "VR"
confirm = true
command = ["frame-remote-steam", "close-vr"]

[[button]]
id = "shutdown"
title = "Shut Down PC"
subtitle = "Powers off in 5 s"
row = "Power"
confirm = true
command = ["sh", "-c", "systemd-run --user --quiet --unit=frame-remote-poweroff sh -c 'sleep 5 && systemctl poweroff' && echo 'Shutting down in 5 seconds.'"]

[[indicator]]
label = "Frame mode"
check = ["test", "-e", "~/.local/state/stream-layout-on"]
```

**Frame: `~/.config/frame-remote/frame.toml`**

```toml
pc = "http://192.168.1.10:38112"
token = "…"                        # the same token, filled in by the installer

[[button]]
id = "wake"
title = "Wake PC"
subtitle = "Wake-on-LAN"
row = "Power"
show = "offline"
wake = "aa:bb:cc:dd:ee:ff"
broadcast = "192.168.1.255"

[toast]
enabled = true
seconds = 4
distance = 1.2
height = -0.22
```

The panel then shows:
- **PC on:** the status lights (SteamVR, Frame mode) and running games, then **Screen** (Frame Mode On/Off), **VR** (Start SteamVR, Close Game, Close SteamVR) and **Power** (Shut Down PC).
- **PC off:** just **Power** with **Wake PC**.

Add the PC Desktop entry from recipe 5 to the Frame's library, and the headset can switch the PC on, stream its desktop or games in the right layout, clean up after VR, and switch it off, all without taking it off.
