# Phase 0 — Base Setup

## Prerequisites

- Raspberry Pi OS Lite **32-bit Bookworm** flashed to SD card
- SSH enabled (user `<user>`, or adjust `install.sh`)
- Network connectivity (WiFi or Ethernet)

## One-command install

```bash
cd /path/to/repo
sudo ./install.sh
```

Re-run safely anytime (idempotent). Use `--update` to refresh packages only.

## What Phase 0 does

1. **Boot tuning** — `arm_freq=1350`, `gpu_mem=16`, `disable_splash=1` in `config.txt`
2. **Network buffers** — sysctl drop-in for streaming + web UI
3. **mDNS** — hostname `safydo`, Avahi publishes `safydo.local`
4. **Minimal services** — SSH + Avahi enabled; desktop/audio daemons disabled
5. **SD card safety** — tmpfs for `/var/log` and `/tmp`; ext4 commit interval tuned
6. **Project deploy** — copies repo to `/opt/rpi-audio-player`

## Verify after install

```bash
# mDNS (from another machine on LAN)
ping safydo.local
avahi-browse -a -t

# SSH
ssh <user>@safydo.local

# Sysctl applied
sysctl net.core.rmem_max

# Hostname
hostname
```

## Why reboot used to break WiFi (fixed)

Bookworm Pi Imager uses **NetworkManager** (`preconfigured.nmconnection`). Our install left **`wpa_supplicant.service` enabled** — both fight for `wlan0` on boot. Before reboot, NM often won; after reboot (more services, timers), wpa_supplicant could win → **no WiFi, no IP**.

Other contributors on first reboot:

1. **`config.txt` overclock / USB OTG** — only applied after reboot; `arm_freq=1350` and `dtoverlay=dwc2` can destabilize BCM43430 WiFi on Zero 2W.
2. **`network-priority.timer`** — USB Ethernet with link-up but no DHCP could steal the default route (fixed: requires IPv4 on eth).
3. **`add-wifi` → wpa_supplicant.conf** — NM ignored it; credentials never used (fixed: `nmcli` on Bookworm).

## Fixes in install.sh

- Mask global `wpa_supplicant` when NetworkManager is present
- `audio-player-network-ready.service` → `network_ready.sh` brings WiFi up if down
- Ethernet priority off by default; eth must have carrier **and** IPv4
- Overclock is **opt-in** (`APPLY_OVERCLOCK=1`); the USB dev-link gadget (dwc2 OTG) installs by default — see `docs/usb-dev-link.md`
- Backend binds `127.0.0.1:8000` (nginx proxy); review fixes in `docs/code-review-fixes.md`
- WiFi powersave drop-in **opt-in** only (`APPLY_WIFI_TUNING=1`); default + `deploy.sh` skip it for reboot safety

Reboot after install is expected and should work:

```bash
sudo reboot
curl -sI https://google.com | head -1
```

## Disabled services (Phase 0)

- `bluetooth` — enabled in Phase 1
- `pulseaudio` / `pipewire` — not used (ALSA direct)
- `triggerhappy`, `hciuart` extras as applicable

## Next: Phase 1

Pair Bluetooth device and test `mpv` → BT sink at best codec.