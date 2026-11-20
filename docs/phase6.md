# Phase 6 — Polish + Web control surfaces

**Pi:** `safydo.local`  
**Prerequisite:** Phases 0–5 deployed, JBL Go 3 paired, streams play from UI.

---

## Session status (2026-07-05)

| Done on Pi | Notes |
|------------|-------|
| Safydo rebrand | hostname, mDNS, UI, `Safydo` AP |
| WiFi install fixes | NM-only, `network_ready`, `sd_recover_wifi.sh` |
| nginx tmpfs fix | `tmpfiles-log-dirs.conf` → `/var/log/nginx` |
| JBL Go 3 | SBC, `primary_mac` saved |
| Stream playback | Works via http://safydo.local (manual start) |
| Reboot test | **Failed** — Pi offline after reboot (2026-07-05); redeploy when back |
| Resume on boot | Code added (`audio.resume_on_boot`) — verify after redeploy |
| Network UI (6b) | **Implemented** in repo — deploy pending |

---

## Already shipped (backend/scripts — UI missing or partial)

| Feature | Backend / scripts | Web UI |
|---------|-------------------|--------|
| Captive portal AP | `captive_portal.sh`, `/api/network/captive/*` | ❌ No panel |
| Add WiFi credentials | `add-wifi` → nmcli / wpa_supplicant, `/api/network/wifi` | ❌ Only `portal.html` |
| Multi-network list | Networks in NM / wpa_supplicant | ❌ No list/switch UI |
| Network status | `/api/network/status` | ❌ Not shown |
| BT reconnect | `/api/bt/connect`, `bt_pair.sh` | ✅ Reconnect button only |
| BT pair / scan / list | `bt_pair.sh` CLI | ❌ No scan/pair/select UI |
| Volume (mpv) | `/api/volume` → mpv only | ✅ Slider |
| HW speaker volume (AVRCP) | — | ❌ Not implemented |

---

## Phase 6 scope

### 6a — USB + system polish
- [x] nginx log dirs on tmpfs boot (`tmpfiles-log-dirs.conf`)
- [x] WiFi reboot-safety scripts (`network_ready`, `sd_recover_wifi`, NM wpa mask)
- [ ] USB multi-FS auto-mount + rescan hooks (udev → backend cache)
- [ ] Safe power loss tuning (`ext4` commit, optional overlayroot doc)
- [ ] Resource profile notes (`htop`, `free`, playing RAM target <200 MB)
- [ ] **Post-reboot smoke:** services, mDNS, stream, BT autoconnect

### 6b — Network panel (WiFi captive + multi-SSID) — **requested**
- [x] **Network** section in React UI (`NetworkPanel.tsx`)
- [x] Show current SSID, IP, gateway, DNS (enhanced `/api/network/status`)
- [x] **Saved networks** list (NM / wpa_supplicant via API)
- [x] Form: add SSID + password → `POST /api/network/wifi`
- [x] Buttons: start/stop captive portal → `POST /api/network/captive/start|stop`
- [x] Help when AP active: join `Safydo` → `http://192.168.4.1/`
- [ ] Optional “forget network” API

### 6c — Bluetooth panel (Web pairing) — **requested**
- [ ] **BT setup** in UI: scan, list paired devices, pair by MAC
- [ ] API wrappers: `POST /api/bt/scan`, `POST /api/bt/pair`, `GET /api/bt/devices`
- [ ] Set primary device → updates `bluetooth.primary_mac` + restart mpv
- [ ] Show negotiated codec / bitrate (from `bt_status.json`)
- [ ] Keep existing reconnect button

### 6d — Volume: software vs hardware follow — **requested**
- [ ] `config/player.yaml`:
  ```yaml
  audio:
    volume: 80
    volume_follow_hardware: false   # sync with BT AVRCP HW buttons
  ```
- [ ] When **off** (default): UI slider → mpv volume only (current behaviour)
- [ ] When **on**: poll AVRCP absolute volume; slider + HW buttons in sync
- [ ] **UI:** toggle near volume slider (“Follow speaker” / “HW vol”)
- [ ] WebSocket status includes `volume_follow_hardware` + `hw_volume`

---

## Phase 7 — Docs & hardening (after 6)

- [ ] README/BUILD test matrix: BT codecs, power loss, USB, network
- [ ] `audio_check` warnings in UI
- [ ] Deploy guard: never rsync empty `primary_mac` over live `player.yaml`
- [ ] Optional: PWA install, graceful shutdown button in UI

---

## Verify Phase 6

```bash
ssh -i ~/.ssh/id_ed25519 <user>@safydo.local \
  'systemctl is-active audio-player-mpv audio-player-backend nginx bluetooth; \
   curl -s http://127.0.0.1/api/network/status; \
   curl -s http://127.0.0.1/api/bt/status'
```

Web: Network panel adds WiFi; BT panel pairs device; volume toggle changes `volume_follow_hardware` in status.

---

## Resume here

1. Run reboot test (see `docs/BUILD.md`)
2. Start **6b** Network panel (highest user-visible value)
3. Then **6c** BT UI, **6d** volume toggle