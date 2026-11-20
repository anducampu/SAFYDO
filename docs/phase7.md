# Phase 7 — Docs, test matrix, hardening

**Pi:** `safydo.local`  
**Prerequisite:** Phase 6 deployed and `phase6_smoke.sh` passes.

---

## Deliverables

| Item | Path |
|------|------|
| Test matrix | This file |
| Deploy script | `scripts/deploy.sh` |
| Config merge | `scripts/merge_player_config.py` |
| PWA manifest | `frontend/public/manifest.webmanifest` |
| Smoke test | `scripts/phase6_smoke.sh` |

---

## Test matrix

Run on Pi after deploy or reboot:

```bash
sudo /opt/rpi-audio-player/scripts/phase6_smoke.sh
```

| Area | Check | Command / expected |
|------|--------|-------------------|
| **Services** | All active | `systemctl is-active bluetooth bluealsa audio-player-mpv audio-player-backend nginx` → `active` |
| **mDNS** | Reachable | `ping -c1 safydo.local` from LAN |
| **Web UI** | Loads | http://safydo.local/ |
| **API** | Health | `curl -s http://127.0.0.1/api/health` → `{"status":"ok"}` |
| **Streams** | Play | UI station click or `SMOKE_PLAYBACK=1 sudo …/phase6_smoke.sh` |
| **BT connect** | Primary up | `bluetoothctl info <MAC>` → `Connected: yes` |
| **BT codec** | Best negotiated | `cat /var/lib/audio-player/bt_status.json` → `is_best_quality: true` |
| **Codec prime** | On connect | `journalctl -u bluealsa \| grep "SBC setup\|LDAC"` → bitrate line after connect |
| **Volume** | mpv + bidirectional HW | UI slider works with “Follow speaker” on; speaker buttons sync back |
| **Stream meta** | ICY / track tags | Now Playing chips: bitrate, format, genre, listeners when stream provides them |
| **Spectrum** | astats RMS → 5 bands | Bars **animate slowly** when WS open; **not music-reactive** per band yet (future work) |
| **EQ** | 5-band mpv filters | Toggle + flat/speaker/headphone presets; `equalizer` at 60–12 kHz |
| **WiFi** | Survives reboot | **PASS 2026-07-06** — `sudo reboot` → ~90s → `safydo.local`, smoke OK |
| **Network UI** | Add / forget WiFi | Network panel; captive AP start/stop |
| **USB** | Auto-mount | Plug FAT32/exFAT/NTFS/ext4 stick → `/media/usb` mounted → UI rescan |
| **RAM** | Under target | `free -m` while playing → ideally <200 MB used |
| **Power loss** | ext4 safe | `tune2fs -l /dev/mmcblk0p2` → `journal_data_ordered`; optional `docs/os-tuning/overlayroot.md` |

---

## Deploy (dev → Pi)

Preserves live `bluetooth.primary_mac` on the Pi:

```bash
./scripts/deploy.sh --build
```

Options: `--skip-install` (rsync + merge only).

Manual equivalent:

```bash
./scripts/build_frontend.sh
rsync … --exclude config/player.yaml …
ssh … 'sudo install.sh --skip-apt'
```

`install.sh` excludes `player.yaml` from rsync and merges via `merge_player_config.py`.

---

## Code review fixes (2026-07-06)

All items from senior code review are implemented. See **`docs/code-review-fixes.md`** for the full list (P0 path traversal, BT boot race, WebSocket perf, deploy merge, API token, frontend UX).

---

## Audio UX (EQ + spectrum + metadata)

- **EQ:** `PUT /api/eq/enabled`, `PUT /api/eq/preset/{id}` — mpv `equalizer` chain at 60 / 250 / 1k / 4k / 12 kHz.
- **Spectrum:** mpv `lavfi astats` + `ametadata` → `/run/audio-player/viz_rms.log`; attached only when `/ws/status` has clients. Single `alsa` output to bluealsa. Bars move slowly; **not true per-band FFT** — see `docs/BUILD.md`.
- **Metadata:** `player.mpv.track_info` in status / WebSocket (ICY title, bitrate kbps, genres, listeners, format).

After deploy (viz or mpv changes):

```bash
sudo systemctl restart audio-player-backend audio-player-mpv
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No audio, metadata only | Dual `ao=alsa,pcm` breaks bluealsa — must use single `alsa` output (see `mpv_daemon.sh`) |
| Visualizer flat / no motion | Open web UI (WS client); check `wc -c /run/audio-player/viz_rms.log` grows; restart backend if socket missing; see `docs/BUILD.md` viz fixes |
| Bars move but ignore music | Expected — overall RMS only; music-reactive spectrum is future work |
| EQ no effect | Enable toggle + preset; restart `audio-player-mpv` |
| Stations don’t play (503) | `sudo systemctl restart bluealsa audio-player-mpv` or `bt_connect.sh` |
| WiFi dead after reboot | `sudo ./scripts/sd_recover_wifi.sh` on SD in PC |
| BT codec shows SBC only | Device may only support SBC (e.g. JBL Go 3); verify `device_codecs` in bt_status.json |
| USB not listed | `sudo /opt/rpi-audio-player/scripts/usb_automount.sh add` |
| `primary_mac` lost after deploy | Use `./scripts/deploy.sh` (not raw rsync of `player.yaml`) |

---

## Phase 7 checklist

- [x] Test matrix (`docs/phase7.md`)
- [x] `deploy.sh` + `merge_player_config.py`
- [x] `audio_check` warnings in UI (`AudioCheckBanner`)
- [x] Shutdown button in UI (footer)
- [x] PWA manifest (Add to Home Screen)