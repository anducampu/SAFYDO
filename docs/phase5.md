# Phase 5 — Network extras

Ethernet priority over WiFi + on-demand WiFi captive portal.

## Ethernet priority

When USB/Ethernet carrier is up, default route prefers `eth*` / `enx*` (metric 100); WiFi stays up as standby (metric 200).

```bash
sudo /opt/rpi-audio-player/scripts/network_priority.sh
cat /var/lib/audio-player/network_priority.json
systemctl status network-priority.timer
```

Toggle in `config/player.yaml`:

```yaml
network:
  ethernet_priority: true
```

## Captive portal

Start AP **Safydo** at `192.168.4.1` for WiFi provisioning:

```bash
sudo /opt/rpi-audio-player/scripts/captive_portal.sh start
# phone joins Safydo → http://192.168.4.1/ or /setup
sudo /opt/rpi-audio-player/scripts/captive_portal.sh stop
```

It also comes up **by itself** when the box has no uplink: `captive_watch.sh` on a
30 s timer raises the AP after three consecutive misses (~90 s), and stands down
once a real uplink returns. It needs a wireless adapter, so an ethernet-only box
never raises one.

CLI add network (no AP):

```bash
sudo /opt/rpi-audio-player/scripts/captive_portal.sh add-wifi "MySSID" "password"
```

List what is on the air (JSON — this is what the portal page's picker reads):

```bash
sudo /opt/rpi-audio-player/scripts/captive_portal.sh scan
```

### Why the network list is a cached survey

A one-radio box **cannot scan while hostapd owns the interface**, so `start` takes
the survey in the last moment the radio is still in client mode and caches it to
`/run/audio-player/captive/scan.json`. While the AP is up, `scan` serves that cache
and never touches the radio — returning in ~0.08 s, so opening the portal page
cannot knock the phone off the network it is being served over. With no AP running,
`scan` sweeps for real. The API reports which you got via `cached`.

### Gotcha — the packaged dnsmasq blocks the portal's DHCP

The `dnsmasq` package enables a **system-wide resolver on :53** with a wildcard
bind, and the portal's own dnsmasq then cannot bind and crash-loops. The failure
looks like a working AP: hostapd broadcasts, a phone associates, gets no lease and
drops seconds later. Three things keep it fixed — `except-interface=lo` in the
portal config (with `bind-interfaces`, dnsmasq still binds loopback, which is what
actually collided), `install.sh` disabling `dnsmasq.service`, and `captive_portal.sh`
stopping it defensively at start.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/network/status` | Priority + captive state |
| GET | `/api/network/wifi/scan` | Nearby networks for the picker (`cached` says live vs pre-AP survey) |
| POST | `/api/network/wifi` | `{"ssid","password"}` — save + stop captive |
| POST | `/api/network/captive/start` | Start AP mode |
| POST | `/api/network/captive/stop` | Stop AP, restore client WiFi |

## Packages

`hostapd`, `dnsmasq`, `wpasupplicant` (offline install if Pi has no apt WAN).