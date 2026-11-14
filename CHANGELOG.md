# Changelog

## 0.1.0 (2026-11-14)

First public release (beta).

- SteamVR dashboard panel on the Steam Frame ("PC Remote" icon in the bottom bar) with buttons that run commands on your Linux PC.
- Buttons and status lights are configured on the PC only (`config.toml`); the headset downloads the layout.
- Built-in Steam helper: Start SteamVR (starts Steam first if it isn't running), Close Game, Close SteamVR, Close All.
- Local buttons that run on the headset itself, including built-in Wake-on-LAN that waits for the PC to come up.
- Feedback on every command: button states, a result banner with the command's last line of output, and a pop-up in front of your eyes.
- Flicker-free drawing through a persistent OpenGL texture.
- Installers for the PC and the Frame, plus `push-to-frame.sh` to install or update the headset over SSH.
