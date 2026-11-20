# Phase 1 — Bluetooth Audio

Pi plays **to** a paired BT speaker/headphone (A2DP source) via **bluealsa** + **mpv**. No PulseAudio/PipeWire.

## Stack

| Component | Role |
|-----------|------|
| `bluetooth` / `bluetoothctl` | Pairing, trust, connect |
| `bluealsa` | ALSA PCM → BlueZ A2DP |
| `mpv` | Playback with `--audio-device=alsa/bluealsa:DEV=MAC,PROFILE=a2dp` |

## Supported codecs (negotiated with device)

| Codec | Typical max bitrate | Pi support |
|-------|---------------------|------------|
| SBC | ~328 kbps | Always (mandatory A2DP) |
| AAC | ~320 kbps | If `bluez-alsa` built with AAC |
| aptX | ~352 kbps | If build + device support |
| aptX-HD | ~576 kbps | Rare on Pi Zero 2W builds |
| LDAC | ~990 kbps | Uncommon on 32-bit images |

Run `play_test.sh` then `bt_codec.sh` to see the negotiated codec.

## Quick test

```bash
# After install.sh on the Pi
sudo systemctl status bluetooth bluealsa

# Put headphones/speaker in pairing mode
sudo /opt/rpi-audio-player/scripts/bt_pair.sh scan
sudo /opt/rpi-audio-player/scripts/bt_pair.sh pair AA:BB:CC:DD:EE:FF

# 30s test stream
sudo /opt/rpi-audio-player/scripts/play_test.sh

# Codec + status JSON
sudo /opt/rpi-audio-player/scripts/bt_codec.sh --update-status
cat /var/lib/audio-player/bt_status.json
```

## Scripts

| Script | Purpose |
|--------|---------|
| `bt_pair.sh scan` | Discover devices |
| `bt_pair.sh pair MAC` | Pair, trust, connect, set primary |
| `bt_connect.sh` | Connect primary device |
| `bt_codec.sh` | Detect codec → JSON |
| `bt_autoconnect.sh` | Boot reconnect (systemd) |
| `play_test.sh` | mpv stream test to BT |

## Troubleshooting

**No `bluealsa` PCM / mpv fails**
```bash
systemctl restart bluealsa
bluealsa-aplay -L
```

**Device pairs but won't connect**
```bash
bluetoothctl info MAC
sudo systemctl restart bluetooth
sudo /opt/rpi-audio-player/scripts/bt_connect.sh MAC
```

**Codec stuck at SBC**
- Many budget speakers only support SBC
- AAC/aptX require device support + bluealsa build flags
- Check: `journalctl -u bluealsa -n 50` during playback

**Auto-reconnect**
- `bluetooth.primary_mac` in `config/player.yaml`
- `audio-player-bt-autoconnect.service` enabled by install.sh