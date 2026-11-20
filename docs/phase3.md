# Phase 3 — Backend API

FastAPI wraps `PlayerService`, BT status, and USB helpers. WebSocket pushes live status.

## Service

| Unit | Role |
|------|------|
| `audio-player-backend.service` | uvicorn on `0.0.0.0:8000` |

```bash
sudo systemctl enable --now audio-player-backend
curl -s http://safydo.local:8000/api/health
```

## REST endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness |
| GET | `/api/status` | Player + Bluetooth snapshot |
| GET | `/api/streams?favorites=true` | Stream list (69 from `stations.json`) |
| GET | `/api/streams/{id}` | Single stream |
| POST | `/api/play/stream/{id}` | Play radio stream |
| POST | `/api/play/url` | `{"url": "..."}` |
| POST | `/api/play/file` | `{"path": "/media/usb/..."}` |
| POST | `/api/transport/pause` | Pause |
| POST | `/api/transport/resume` | Resume |
| POST | `/api/transport/toggle` | Toggle pause |
| POST | `/api/transport/stop` | Stop |
| PUT | `/api/volume` | `{"level": 0-100}` |
| GET | `/api/usb` | Cached USB tracks |
| POST | `/api/usb/scan` | Rescan `/media/usb` |
| GET | `/api/eq` | Active EQ preset (stub) |
| PUT | `/api/eq/enabled` | `{"enabled": true/false}` |
| GET | `/api/bt/status?refresh=true` | BT codec/device |
| POST | `/api/bt/connect` | `{"mac": "optional"}` |

## WebSocket

`ws://safydo.local:8000/ws/status` — JSON status every ~1s while connected.

## Stations import

```bash
sudo /opt/rpi-audio-player/scripts/import_stations.py
sudo /opt/rpi-audio-player/scripts/playerctl.py streams --favorites
sudo /opt/rpi-audio-player/scripts/stream_test.sh jazz24
```

`install.sh` runs import automatically when `stations.json` is present.

## Internet note

Stream playback needs outbound connectivity from the Pi. Until WAN works, test with:

```bash
curl -s -X POST http://safydo.local:8000/api/play/file \
  -H 'Content-Type: application/json' \
  -d '{"path":"/usr/share/sounds/alsa/Front_Center.wav"}'
```