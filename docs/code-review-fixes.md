# Code review fixes (2026-07-06)

Senior engineer review applied in priority order. Deploy with `./scripts/deploy.sh --build` after merging.

## P0 — Security & correctness

| # | Fix | Files |
|---|-----|-------|
| 1 | USB-only `play_file` via `_resolve_usb_path()` | `backend/services/player.py` |
| 2 | Reject `file://` and non-http(s) in `play_url()` | `backend/services/player.py` |
| 3 | Boot resume uses `idle-active` not `idle` | `backend/main.py` |
| 4 | `set_primary()` validates MAC, returns real `ok` | `backend/services/bluetooth.py` |

## P1 — Reliability & performance

| # | Fix | Files |
|---|-----|-------|
| 5 | `_wait_for_playback()` before `require_playback` verify | `backend/services/audio_verify.py` |
| 6 | Light `audio_check` on WebSocket; deep verify on `/api/status` only | `backend/services/system.py`, `backend/main.py` |
| 7 | mpv waits for BT autoconnect; `flock` on bluetoothctl (no `killall`) | `scripts/mpv_daemon.sh`, `scripts/_common.sh` |
| 8 | `asyncio.to_thread()` for blocking play/BT/network handlers | `backend/main.py` |
| 9 | Deploy merge preserves full `bluetooth`, `network`, `audio` sections | `scripts/merge_player_config.py` |
| 10 | `play_file` raises on verify failure (same as streams) | `backend/services/player.py` |
| 11 | `toggle_pause` uses `get_property_safe` | `backend/services/player.py` |
| 12 | Corrupt `player_state.json` no longer breaks status | `backend/services/player.py` |
| 13 | N+1 `bluetoothctl info` removed from device list | `backend/services/bluetooth.py` |

## P2 — Hardening

| # | Fix | Files |
|---|-----|-------|
| 14 | Optional `PLAYER_API_TOKEN` + `X-Safydo-Token` on privileged routes | `backend/main.py`, `frontend/src/api/client.ts` |
| 15 | WiFi password via temp file (not argv) | `backend/services/network.py`, `scripts/captive_portal.sh` |
| 16 | Backend binds `127.0.0.1` (nginx proxy only) | `systemd/audio-player-backend.service` |
| 17 | Atomic `player.yaml` writes | `scripts/_yaml_edit.py` |
| 18 | AVRCP transport path cached 30s | `backend/services/volume_hw.py` |
| 19 | `test_tone` restores previous playback | `backend/services/audio_verify.py` |

## P3 — Frontend & scripts

| # | Fix | Files |
|---|-----|-------|
| 20 | Central API error parsing; stream id encoded | `frontend/src/api/client.ts` |
| 21 | Volume: no touch double-commit, rollback, follow-speaker disable | `frontend/src/components/VolumeSlider.tsx` |
| 22 | Shutdown via `api.shutdown()` with busy guard | `frontend/src/App.tsx` |
| 23 | Stream list fetch cancellation | `frontend/src/components/StreamBrowser.tsx` |
| 24 | WebSocket unmount guard | `frontend/src/hooks/usePlayerStatus.ts` |
| 25 | Open WiFi networks allowed in UI | `frontend/src/components/NetworkPanel.tsx` |
| 26 | USB automount: flock, USB-by-id only, dynamic UID | `scripts/usb_automount.sh` |
| 27 | Smoke test playback optional (`SMOKE_PLAYBACK=1`) | `scripts/phase6_smoke.sh` |

## Optional API token

When `PLAYER_API_TOKEN` is set in the backend environment, privileged endpoints require header `X-Safydo-Token`. Store token in browser:

```javascript
sessionStorage.setItem('safydo_token', 'your-secret');
```

Privileged routes: shutdown, WiFi add/forget, captive portal, BT pair/primary.

## WiFi reboot safety (2026-07-06)

`wifi-powersave-off.conf` is **opt-in** (`APPLY_WIFI_TUNING=1`). Default install and `deploy.sh` use `SKIP_WIFI_TUNING=1` and **remove** the drop-in if present — matches `sd_recover_wifi.sh`.

`install.sh --skip-apt` (deploy) **does not re-mask** `wpa_supplicant`; removes mask if present so `sudo reboot` survives after SD recovery.

---

## Viz / spectrum fixes (2026-07-06)

| # | Fix | Files |
|---|-----|-------|
| V1 | Backend must not use `RuntimeDirectory=audio-player` (wiped mpv.sock + viz log on restart) | `systemd/audio-player-backend.service` |
| V2 | Truncate viz log only before `af set`, not after mpv opens it | `backend/services/spectrum.py`, `backend/services/eq.py` |
| V3 | Re-attach astats after play; heal stale viz state | `backend/services/player.py` |
| V4 | Fix tick/levels lock deadlock; tail-read RMS log | `backend/services/spectrum.py`, `backend/main.py` |
| V5 | Stronger frontend bar wobble; `live` from spectrum.active | `frontend/src/components/Visualizer.tsx`, `NowPlaying.tsx` |

**Known limitation:** bars animate slowly but are not music-reactive (single overall RMS). Next: `docs/BUILD.md` → *Future: music-reactive spectrum*.

---

## Verification

```bash
# On dev machine
python3 -m compileall backend scripts
./scripts/build_frontend.sh

# On Pi after deploy
sudo /opt/rpi-audio-player/scripts/phase6_smoke.sh
SMOKE_PLAYBACK=1 sudo /opt/rpi-audio-player/scripts/phase6_smoke.sh  # optional stream test
sudo reboot  # WiFi + BT boot test
```