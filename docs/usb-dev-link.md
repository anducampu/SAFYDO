# USB dev link + network dongles + onboard WiFi off

Why: the Zero 2W has **one 2.4 GHz radio shared between WiFi and BT** — WiFi
traffic steals A2DP airtime (audible glitches, `br-connection-busy`). The fix:
disable onboard WiFi entirely and get internet from a USB dongle instead. BT
is unaffected (`disable-wifi` cuts only the WLAN half of the combo chip).

Before losing WiFi we need a way in. That's the **USB dev link**.

## 1. USB dev link (serial + SSH over one cable)

`dtoverlay=dwc2,dr_mode=otg` + a configfs composite gadget
(`scripts/usb_gadget.sh`, `usb-gadget.service`) present two devices to a
tethered PC:

| Function | Pi side | PC side | Use |
|----------|---------|---------|-----|
| ACM serial | `/dev/ttyGS0` (getty) | `/dev/ttyACM0` | `screen /dev/ttyACM0 115200` → login console |
| ECM ethernet | `usb0` @ `10.55.0.1/24` | USB NIC, DHCP `10.55.0.x` | `ssh <user>@10.55.0.1` (or `safydo.local`) |

- **Cable into the data port** — the inner micro-USB labeled "USB", not "PWR IN".
  Must be a data cable (charge-only cables enumerate nothing).
- The Pi's DHCP (`usb-gadget-dhcp.service`, dnsmasq on usb0 only) pushes **no
  gateway and no DNS** — it never hijacks the PC's internet.
- `dr_mode=otg` = the same port still works as **USB host** through an OTG
  adapter (dongles, USB sticks). Host and gadget are mutually exclusive at any
  moment — pick by cable.
- Windows PCs don't speak ECM natively — serial console still works; use Linux
  for the SSH path.

Check on the Pi: `sudo /opt/rpi-audio-player/scripts/usb_gadget.sh status`

### GPIO UART fallback (last resort besides SD surgery)

`enable_uart=1` is set; `console=serial0,115200` was already in cmdline.txt.
With a USB-TTL adapter (3.3 V!): GND→pin 6, RX→pin 8 (Pi TXD), TX→pin 10
(Pi RXD), then `screen /dev/ttyUSB0 115200`. Uses the mini-UART — Bluetooth
stays on the PL011, unaffected.

## 2. Network dongles (plug and forget)

NetworkManager handles hotplug natively; the repo work was compat fixes
(avahi publishes on all interfaces now; `network_ready.sh` and the backend no
longer assume `wlan0`).

| Dongle | Interface name | What happens |
|--------|----------------|--------------|
| USB ethernet | `enx<mac>` (or `eth0`) | NM auto-creates a DHCP connection ("Wired connection 1") on plug |
| USB WiFi | `wlx<mac>` | NM autoconnects to saved SSIDs — the `preconfigured` profile is not interface-pinned |

- Ethernet beats WiFi automatically when both are up (NM route metrics
  ~100 vs ~600).
- Swap behavior: pull the ethernet dongle → route drops; plug the WiFi dongle
  → NM associates and restores the default route. Nothing to configure.
- Diagnose a new dongle: `sudo /opt/rpi-audio-player/scripts/dongle_status.sh`
  — if the dongle shows in lsusb but no interface appears, the kernel lacks
  its driver/firmware (typical for very new Realtek chips; check the kernel
  messages section it prints).
- The captive portal (Phase 5, off by default) assumes `wlan0` and won't run
  on a `wlx*` dongle — known limitation.

## 3. Disabling onboard WiFi

**Only after the dev link is verified** (SSH over USB tether works):

```bash
sudo /opt/rpi-audio-player/scripts/wifi_onboard.sh disable --confirm
sudo reboot
```

The script refuses without `--confirm` and checks the gadget is staged
(`FORCE=1` overrides). It appends a marked block to config.txt:

```
# --- SAFYDO onboard-wifi-disable begin ---
dtoverlay=disable-wifi
# --- SAFYDO onboard-wifi-disable end ---
```

Undo: `sudo wifi_onboard.sh enable` + reboot, or — locked out — mount the SD
boot partition on a PC and delete that block from `config.txt`
(`scripts/sd_recover_wifi.sh` also needs that block gone to work).

After disabling: deploys go over the dongle IP or the USB tether
(`PI_HOST=10.55.0.1 ./scripts/deploy.sh`). `safydo.local` resolves on
whichever link is active (avahi publishes everywhere).

## Recovery ladder (most convenient first)

1. `ssh <user>@safydo.local` / dongle IP — normal operation
2. USB tether → `ssh <user>@10.55.0.1`
3. USB tether → `screen /dev/ttyACM0 115200` (serial login)
4. USB-TTL on GPIO pins 6/8/10 → `screen /dev/ttyUSB0 115200`
5. SD card in PC → delete the `disable-wifi` block in `config.txt`
