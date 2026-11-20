# Phase 2 — Core Player

mpv JSON IPC daemon + stream/USB control via `playerctl.py`.

## Services

| Unit | Role |
|------|------|
| `audio-player-mpv.service` | Idle mpv with IPC socket at `/run/audio-player/mpv.sock` |
| `audio-player-bt-autoconnect.service` | Reconnect JBL/BT before mpv starts |

## CLI

```bash
sudo systemctl start audio-player-mpv
sudo /opt/rpi-audio-player/scripts/playerctl.py status
sudo /opt/rpi-audio-player/scripts/playerctl.py streams
sudo /opt/rpi-audio-player/scripts/playerctl.py play-stream jazz24
sudo /opt/rpi-audio-player/scripts/playerctl.py pause
sudo /opt/rpi-audio-player/scripts/playerctl.py toggle
sudo /opt/rpi-audio-player/scripts/playerctl.py volume 65
sudo /opt/rpi-audio-player/scripts/playerctl.py usb-scan
sudo /opt/rpi-audio-player/scripts/playerctl.py eq status
```

## Offline mpv install (Pi has no internet)

On **dev machine**:

```bash
chmod +x scripts/offline_fetch_debs.sh
./scripts/offline_fetch_debs.sh mpv
cd /tmp/pi-debs && python3 -m http.server 8765
```

On **Pi**:

```bash
mkdir -p ~/pi-debs && cd ~/pi-debs
for f in $(curl -s http://<dev-box-ip>:8765/ | grep -oP 'href="\\K[^"]+\\.deb'); do
  curl -fsSO "http://<dev-box-ip>:8765/$f"
done
sudo dpkg -i *.deb
sudo /opt/rpi-audio-player/install.sh --skip-apt
```

## Internet note

Internet **radio streams** need outbound connectivity from the Pi. If only LAN works, test with a local file:

```bash
sudo /opt/rpi-audio-player/scripts/playerctl.py play-file /usr/share/sounds/alsa/Front_Center.wav
```

## State files

| Path | Content |
|------|---------|
| `/var/lib/audio-player/player_state.json` | Last play source/metadata |
| `/var/lib/audio-player/usb_tracks.json` | USB scan cache |