# Second target: Raspberry Pi 3 A+ on Raspberry Pi OS Lite 32-bit (Trixie)

SAFYDO supports two machines from one repo. `install.sh` detects **board × OS**
and branches; everything else (backend, frontend, BT stack, player, services) is
identical on both.

| | Zero 2 W / Bookworm | 3 A+ / Trixie |
|---|---|---|
| SoC | BCM2710A1 4×A53 @1.0 GHz | BCM2837B0 4×A53 **@1.4 GHz** |
| RAM | 512 MB | 512 MB |
| Kernel (32-bit image) | kernel7l | kernel7l (`arm_64bit` defaults to 0 on BCM2837) |
| Radio | 2.4 GHz 802.11n + BT 4.2 — **one shared radio** | **2.4 + 5 GHz 802.11ac** + BT 4.2 |
| USB | micro-USB OTG data port | **one USB-A host port**, no usable gadget mode |
| Power | micro-USB | **5 V / 2.5 A** |
| Python | FastAPI 0.92, Pydantic 1.10 | FastAPI 0.115, Pydantic 2.x, Python 3.13 |
| BlueZ | 5.66 | **5.82** |
| bluez-alsa (packaged) | 4.0.0 — SBC + LDAC, no `bluealsa-cli` | 4.3.1-3 — + aptX/aptX-HD, **ships `bluealsa-cli`**, still no AAC |
| fdk-aac | built from source (2.0.3) | **also built from source** — `libfdk-aac-dev` is not in Raspbian trixie armhf (verified on hardware 2026-08-01) |
| /tmp | disk → our fstab tmpfs entry | tmpfs by default; our fstab entry caps it at 64 MB |

## What install.sh does differently

Detection lives in `scripts/platform.sh` (`platform_board`, `platform_os`, and
`board_*` predicates); `scripts/_common.sh` sources it so every script has it.
Override for testing with `SAFYDO_BOARD=` / `SAFYDO_OS=`.

- **USB gadget dev link — Zero 2 W only.** On the 3 A+ the OTG ID pin is tied to
  0 V (host-only unless `dr_mode=peripheral` *and* a USB-A-to-A cable with the
  power line cut), and the `dwc2` overlay would replace the FIQ-driven `dwc_otg`
  host driver — a throughput hit on the one port the network dongle streams
  over. `apply_usb_gadget` returns early; `enable_uart=1` (GPIO pins 6/8/10)
  is deployed instead as the lockout fallback.
- **Boot config fragments are model-filtered** (`[pi02]` / `[pi3]`, closed with
  `[all]`), so an SD card moved between boards cannot apply the wrong tuning.
- **Overclock is per board.** Zero 2 W: `arm_freq=1350` (1000 stock). 3 A+:
  **none by default** — it already runs 1400 MHz and has no heatspreader, so
  `APPLY_OVERCLOCK=1` (1500 + `over_voltage=2`) is only worth it with a heatsink.
- **Packages**: `libfdk-aac-dev` is probed for, not assumed (absent on Raspbian trixie); `python3-lgpio` installed
  explicitly on both (the Blinkt LED meter never had it in the list).
- **Codec build** (`scripts/build_bluealsa.sh`): on trixie the fdk-aac source
  stage is skipped because apt supplies the same 2.0.3, so the build is
  ~10-15 min instead of 30-45.
- **`SAFYDO_HOSTNAME`** overrides the default `safydo` — see below.

## Bring-up

1. **Flash** Raspberry Pi OS Lite **32-bit (Trixie)** with Imager.
   Set hostname (`safydo3a` if the Zero 2 W stays on the LAN — see *Hostnames*),
   user `<user>`, the same SSH public key, WiFi left unconfigured.
2. Plug the **USB ethernet dongle** into the single USB-A port and boot.
   Find the address: `ping safydo3a.local`, or the router's DHCP list.
3. **First install — must be a full one.** `deploy.sh` always runs
   `install.sh --skip-apt`, so the packages have to be installed once by hand:
   ```sh
   # dev box: push the tree, don't run the installer yet
   PI_HOST=<ip> ./scripts/deploy.sh --build --skip-install
   # on the Pi: full install (apt + codec build, ~15-20 min)
   ssh <user>@<ip> 'sudo SAFYDO_HOSTNAME=safydo3a /opt/rpi-audio-player/install.sh'
   ```
   `install.sh` logs the detected platform on the first line — check it says
   `[pi3aplus/trixie]` before anything else.
   Every later deploy is just `PI_HOST=<ip> ./scripts/deploy.sh --build`.
4. **Pair a speaker** in the web UI (Bluetooth panel → scan ≥28 s for classic
   BR/EDR devices), then confirm the codec chip reads SBC-XQ.
5. **Optional — drop onboard WiFi** once the dongle is proven:
   ```sh
   sudo /opt/rpi-audio-player/scripts/wifi_onboard.sh disable --confirm && sudo reboot
   ```
   The guard here is board-aware: on this board it requires a USB uplink with
   carrier + IP (there is no gadget dev link to fall back on).

## Things that bite

- **One USB port.** The dongle occupies it, so USB-stick playback needs a
  powered hub — which also has to fit inside the 5 V/2.5 A budget. The
  alternative: this board is **dual-band**, so leaving onboard WiFi on and
  pinning the router to 5 GHz keeps BT alone on 2.4 GHz *and* frees the port.
  That is the one genuine hardware advantage the 3 A+ has over the Zero 2 W.
- **Hostnames.** Two boards both called `safydo` on one LAN → avahi renames one
  to `safydo-2.local` and SSH known_hosts breaks. Give the second board
  `SAFYDO_HOSTNAME=safydo3a` at install time (it sets `/etc/hostname`,
  `/etc/hosts` and the mDNS name).
- **BlueZ 5.82 vs 5.66 — text parsing is fine** (verified on hardware
  2026-08-01: paired and streamed with no script changes). Note `btmgmt con-info`
  does **not** exist here; for a connected link use `hcitool lq` / `hcitool rssi`.
  If pairing or codec selection ever misbehaves, diff the raw output first:
  ```sh
  bluetoothctl show; bluetoothctl info <MAC>; bluetoothctl devices Paired; btmgmt info
  ```
- **A2DP stalls mid-stream — OPEN BUG (2026-08-08).** Audio stops for a moment,
  and sometimes for good, on this board while the Zero 2 W streaming to the *same
  speaker at the same codec* never does. Everything else was eliminated by
  measurement: link quality pegged at `255` throughout, stream cache growing to
  146 s, load ~0.5, `throttled=0x0`, and it still happened on ethernet with **zero
  radios in the box** — so it is not RF, network, CPU, power or the dongle. The
  positive finding: **`btmon` records zero ACL packets while `bluealsa-cli` reports
  the transport `Running: true`**. bluealsa accepts PCM data and stops feeding the
  A2DP socket, logging nothing. Reproduced with mpv stopped and `speaker-test`
  writing straight to the sink, so it is not mpv either. Next: run bluealsa
  verbose, drop `bluealsa-aplay` (pointless on a source-only box, one less client),
  and compare the `/usr/local` build against Trixie's packaged 4.3.1-3.
- **The onboard radio comes back rfkill soft-blocked.** Re-enable it with
  `wifi_onboard.sh enable` and it boots `Soft blocked: yes` with NM reporting
  `WIFI: disabled`. Self-inflicted: `network_ready.sh` runs `nmcli radio wifi off`
  when no adapter is present, that state persists, and NM re-applies it to a radio
  that appears later. Fixed 2026-08-08 by asserting the radio state in **both**
  directions (the "on" half used to sit below the `have_default_route` early exit,
  so it never ran on a box already online over ethernet).
- **The `rtl8192cu` USB WiFi dongle is not trustworthy here.** It wedged the
  dwc_otg host port (`device descriptor read/64, error -71`, then a failed
  `attempt power cycle`) so that re-seating produced *no kernel event at all* and
  only a reboot revived the port. Separately it hung the link silently: the
  interface kept carrier, IP and route while passing nothing, with no kernel or NM
  log for eight minutes. Prefer ethernet, or a 5 GHz dongle with a better chipset.
- **armhf Trixie** gets less testing than arm64 and some libs are still
  mid-`t64` transition. Stick to packaged dependencies; the codec build is the
  only thing compiled locally.
- **If the pristine bluez-alsa 4.3.1 build misbehaves against BlueZ 5.82**,
  rebuild from Debian's patched source instead (needs `deb-src` enabled):
  ```sh
  sudo apt-get build-dep bluez-alsa && apt-get source bluez-alsa
  # add --enable-aac to debian/rules configure flags, then dpkg-buildpackage -b
  ```

## Recovery

- GPIO serial console: pins 6 (GND) / 8 (TX) / 10 (RX), 115200, USB-TTL adapter.
- Onboard WiFi back on: `sudo scripts/wifi_onboard.sh enable` + reboot, or delete
  the `SAFYDO onboard-wifi-disable` block from `config.txt` with the SD in a PC.
- `scripts/dongle_status.sh` explains what the kernel/NetworkManager did with a
  dongle; `scripts/diag.sh` dumps BT/audio/network state.
