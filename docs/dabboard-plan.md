# DABBoard FM/DAB+ receiver — implementation plan

**Status:** FM option built and deployed to the 3 A+ (2026-10-31), not yet committed; user guide in `docs/radio.md`. **DAB+ is unproven** and not offered. See "Built" below for what has and hasn't been run on hardware.  
**Goal:** an optional receiver module, off by default and switched on from Settings, starting on the
Pi 3 A+. The uGreen DABBoard's audio reaches the Pi over I2S and plays on the BT speaker like any
other stream, with an FM and DAB+ panel in the web UI.

Tags: **[manual]** vendor document, **[source]** read from code or a chip document but not run,
**[inferred]** a conclusion from those. Flags and JSON fields below have not been executed, so
confirm them with `radio_cli --help` on the board.

## Decisions

- **The 3 A+ has no Blinkt.** The strip is removed and the tuner is the only thing on its header. The
  jumper-wire route for running both is dropped. The Zero 2W keeps its Blinkt, so it stays out of
  scope until a later port.
- **Audio goes through the existing mpv**, never through a second output. EQ, VU, volume and Now
  Playing stay, and the single-`ao` rule holds.
- **Audio quality rule:** underruns get more buffer or a fix to the cause, never a lower codec or
  bitrate.
- **Vendor binaries stay out of the repo and the public mirror.** The user downloads them.
- **Not offered:** AM, HD Radio and slideshow. The chip or `radio_cli` doesn't provide them.

## Still undecided

- **Power-cut risk.** With RSTB high, the manual warns that "any voltage fluctuations on the power
  inputs of the Si468x must be avoided, otherwise this can result in permanent damage". SAFYDO assumes
  the plug gets pulled, and no code runs after a cut. Software can limit exposure to listening time
  and no further. The stated worst case is the €44.80 module, not the Pi [inferred]. **Needed before
  gate S3.**
- **FM plus DAB+, or DAB+ only**, if gate S4 passes for only one mode.

## The board in short

| Item | Value |
|---|---|
| Chip | Si468x, sources disagree between Si4684, Si4688 and Si4689. Read it with `radio_cli -i` |
| Control | SPI0 CE0 (`/dev/spidev0.0`), closed `radio_cli`, runs as root, exits leaving the chip running |
| Audio | I2S with the chip as clock master; the Pi only captures. ALSA card `dabboard` |
| Overlay | Stock `dtoverlay=ugreen-dabboard`, present in `rpi-6.18.y` |
| Pins (BCM, mapped from physical) | I2S 18/19/20, RSTB **23**, INTB 25, SPI 8/9/10/11, optional flash CS 7 |
| Power | 3.3 V only, at most about 76 mA. No 5 V connection |
| Firmware | Inside `radio_cli`, loaded to chip RAM. `-b` must rerun after every reset or power loss |
| Presence | No HAT EEPROM, and the ALSA card appears without the board [inferred]. Only SPI can prove it's there |
| Antenna | Passive 50 Ω SMA female, none included. The window wire antenna doesn't work for FM |

## Phase 0 — code fixes, no hardware needed

These are worth having even if the receiver never ships.

- [x] **GPIO 23 lockout.** Removing the strip doesn't fix this: the backend claims the pin and clocks a
      frame at every start and stop, even with LEDs off (`backend/main.py:66,248`,
      `backend/services/leds.py:75-80,103`), and the UI still offers the switch with nothing fitted
      (`leds.py:78`).
  - Build `BlinktLeds` only when LEDs are enabled, on the worker thread (lgpio handles are thread-affine).
    Close the handle when they are disabled. Send no frame unless the option is on.
  - Make `radio.enabled` and `leds.enabled` mutually exclusive in `put_config` (409), and have the LED
    driver refuse independently when the radio block is in `config.txt`.
  - Hide the Blinkt section while radio is enabled (`SettingsModal.tsx:226-256`).
  - A kernel line claim will not protect the pin: `radio_cli` uses wiringPi, which probably writes the
    GPIO registers directly [inferred]. A backend restart while listening must not touch RSTB.
- [x] **Deploy resets settings.** Add `radio`, `leds` and `sources` to `PRESERVE_SECTIONS`
      (`scripts/merge_player_config.py:17`). Without this, a deploy turns the radio off while its
      overlay stays in `config.txt`.
- [x] **Mirror gate.** `scripts/make_public.sh` passes binaries: `grep -I` skips them and the review
      gate only covers images. Fail on any ELF or non-allowlisted binary, and add matching `.gitignore`
      entries.

## Phase 1 — read-only check of the 3 A+

No bus traffic, no config changes. Confirm the board with `uname -r` (6.18.x).

- [x] `leds.enabled` is false in the live `player.yaml`. If it's true, the backend is driving the tuner's
      reset line now, so turn it off first.
- [ ] Status LED off (user to look), and `pinctrl get 23` reads low (done: `op lo`).
- [x] Read `config.txt`, `gpioinfo`, `arecord -l`, `/proc/device-tree/hat/` (expected absent),
      `vcgencmd get_throttled`.
      Result: no radio block, overlay file present, no capture card, no `/dev/spidev*`, no HAT node,
      `throttled=0x0`.
- [ ] Read the BT stall log (done), then collect a wedge baseline with the chip idle
      (`scripts/bt_stall_watch.py`, hours per wedge). A wedge during a later soak means nothing without it.

## Phase 2 — hand-run test, before writing the option

Needs approval for the `config.txt` edit, the reboot and the first RSTB raise. Download the vendor bundle
outside the repo. Fit an FM-capable antenna first, keep power stable, and don't swap USB adapters while
the board's LED is lit.

```ini
# BEGIN SAFYDO RADIO
[pi3]
dtparam=spi=on
dtparam=i2s=on
dtoverlay=ugreen-dabboard
[all]
# END SAFYDO RADIO
```

The vendor also lists `i2c_arm` and `audio`. Add them only if tune or capture fails.

| Gate | Step | Pass | If it fails |
|---|---|---|---|
| S1 | Add the block, reboot | `dabboard` in `arecord -l`, `/dev/spidev0.0` exists, `hci0` up, 0 failed units | Remove the block, read `dtdebug` |
| S2 | Install WiringPi, unpack bundle, `ldd radio_cli`, `--help` | Libraries resolve, v3.2.1 help text | **Stop.** Feature blocked, ask the vendor |
| S3 | `radio_cli -i`, then `-k` (first RSTB raise) | Part reported, LED lit then off, `pinctrl get 23` low after `-k` | RSTB stays high: write a helper that drives it low. No SPI response: check the fit, then the vendor |
| S4 | `-b D -o 1 ...`, then `-b F -o 1 ...`, each with a 30 s `arecord -D hw:CARD=dabboard,DEV=0 -f S16_LE -r 48000 -c 2` to a file | Clean stereo at correct pitch in **both modes** | FM fails, DAB works: ask the vendor, then offer DAB+ only or stop. Silent on both: stop |
| S5 | Repeat capture with `S32_LE`, inspect low bits | Real bit depth known | Keep `S16_LE` if it's the only coherent format, and ask the vendor |
| S6 | Stop the mpv unit, play `av://alsa:hw:CARD=dabboard,DEV=0` to the bluealsa device, then through the real daemon | Audible, SBC-XQ unchanged, EQ and VU working | Named `pcm.dab`, `sysdefault`, FIFO from `arecord`, then `snd-aloop`. Only all failing is a no-go |
| S7 | With capture open: retune, change service, switch modes, `-k` | `-EIO` and clock behaviour recorded, ordered restart under 5 s (proposed) | UX work, not a no-go |
| S8 | Soak about 4 h per mode (proposed), measuring drift | No unexplained gaps, ppm known, RAM under 200 MB | Speed controller or more buffer. `hardware error 0x00` is the known BT wedge, so record it separately |

**Build the option if** S1 to S4 pass for a mode you want, one capture route passes S6, and the power-cut
risk is accepted. Vendor questions to send first are in the research report.

### Results (3 A+, 2026-10-31)

| Gate | Result |
|---|---|
| S1 | Pass. `dabboard` card, `spidev0.0`, `hci0` up, 0 failed units |
| S2 | Pass with `wiringpi_3.20_armhf.deb` (official release, SHA-256 checked). 32-bit `radio_cli` v3.2.1 at `/usr/local/lib/ugreen-dab+/` |
| S3 | Pass. **Si4688**, FMHD firmware 5.1.3. `-i` needs a booted chip, so it prints no part number until `-b` has run. `-k` takes RSTB low itself, so no helper is needed |
| S4 FM | Pass, user-confirmed by ear. RFI on 93.5 (`-b F -o 1 -F 93500 -l 63`): RSSI 58-60, SNR 25-26, RDS text and PI valid, 30 s at -20 dBFS, no clipping, L and R nearly identical on this programme, so stereo is not yet shown |
| S4 DAB+ | **Unproven.** `-b D -o 1 -u -j` takes 5 s, 41 channels, no lock or FIC anywhere, SNR flat at -10. The user knows of no coverage, so an absent signal and a broken path look the same here |
| S5 | **16-bit.** `S16_LE` is real audio. `S32_LE` gives `-EIO` with the clock running. `S24_LE` runs but only its low 8 bits are ever set. Use `S16_LE` |
| S6 | **The direct route passed by ear but is unusable:** `av://alsa` spins mpv's `demux` thread at 100 % and wedged the BT controller in 22-73 s (3 of 4 tries). **Use the FIFO route:** `arecord -t raw` into a FIFO, mpv `demuxer=rawaudio`. About 10 % CPU, no faults. SBC-XQ unchanged, VU tap reads -17 dB |
| S7 | Retune 0.6 s, clock survives. `-k` with capture open: mpv drains, logs `-EIO`, then sits frozen with `core-idle` true, `idle-active` false and the path still set, so it looks like playing. `-b D` with capture open freezes the same way. Ordered restart reaches advancing `time-pos` in **2.28 s** (chip 1.36 s), under the 5 s target |
| S8 | Not run |

**BT wedge data:**
- Chip in reset, online streams: 1 wedge in 3 h (15:15, 58 min in, fault score 6/3, watchdog rebooted).
- Chip running, `av://alsa`: faults 73 s, about 30 s and 22 s after start, each needing a reboot. The one clean try (about 90 s of S7) kept restarting the reader.
- Chip running, FIFO route: see Phase 5. The wedge can still be the known 3 A+ one, so hours per wedge are still needed.
- Evidence: `/var/tmp/safydo-btwedge-2026-10-31*/` on the board. What the spin does to the controller isn't known: 3 cores stay free and `throttled=0x0`.

### Built (2026-10-31)

Backend `backend/services/radio.py`, API, FM panel, Settings section, `scripts/radio_setup.sh`,
`scripts/radio_overlay.sh`, `systemd/audio-player-radio*.service`.

**Run on the real board, all passing, 0 controller faults:** play, tune, seek, scan (15 stations in 140 s), stop,
radio to stream and back, link drop (`bluetoothctl disconnect`, back in 17 s), backend restart while listening
(no gap), EQ on radio, `radio_setup.sh`, Settings off and on with reboots, reboot while listening (tuner stays
off, no online stream returns), the resume option, full `deploy.sh` keeping stations.

**Fixed along the way:** `options/demuxer` restored to the invalid `auto`, which broke every stream after radio;
deploys overwriting UI-edited stations and EQ (`deploy.sh`, `install.sh`, and an import on every install);
`audio.resume_on_boot: false` read as true; mpv cache and VU branch memory (see `docs/radio.md`).

**Not established:** stereo on a real stereo broadcast, hours per wedge on the FIFO route (longest clean run so
far is short), clock drift over an hour or more, a real power-off of the speaker on this final build, and the
Zero 2W's Blinkt after the LED worker change.

## Phase 3 — backend

- [ ] `backend/services/radio.py`: one background thread with a lock, at most one `radio_cli` call every
      several seconds, results cached in `full_status`. Never call it from the 1 Hz status path. It is a
      service, not a source plugin (`backend/sources/base.py` allows HTTP GET only).
- [ ] `play_radio()` and `source="radio"`: `play_url` and `streams.json` require http(s)
      (`player.py:90-91,154-155`). Set global `options/*` properties before the load, because `loadfile`
      arguments differ between mpv 0.35 and 0.40, and restore them on exit. Include
      `demuxer-max-back-bytes`: PCM fills the back-buffer 12× faster than a 128 kbps stream.
- [ ] Liveness: `time-pos` advancing, chip status and RF lock. Today a dead capture reports as playing
      (`audio_verify.py:61-69`).
- [ ] Stopped-clock recovery, in order: stop capture, run `radio_cli`, confirm chip running, reopen capture,
      verify `time-pos`. FFmpeg recovers only `-EPIPE`; a stopped clock gives `-EIO` after
      `max(100 ms, buffer × 1.1)` [source]. Mute through it, and don't re-upload firmware to a running chip.
- [ ] Drift: if S8 needs it, read `demuxer-cache-duration` at 1 Hz, hold about 1 s, set `speed` to
      1 ± 0.0002 with `audio-pitch-correction=no` [inferred, nobody found doing it].
- [ ] Power-cut limits: Stop means `radio_cli -k`, not just stopping mpv. Add `ExecStop` in
      `systemd/audio-player-radio.service` and the `safe_shutdown.sh` path. Refuse to boot the chip under
      live undervoltage. Radio stays out of resume-on-boot by default, and a BT reconnect never boots the
      chip unless the user was listening (`system.py:32-71`).
- [ ] Option states, derived on read and never stored:

| State | Condition | UI |
|---|---|---|
| `unsupported` | `board_supports_radio` false (`scripts/platform.sh`, 3 A+ only for now) | Hidden or explained |
| `disabled` | Default | Switch off. No GPIO, SPI, panel, process or RAM use |
| `setup_needed` | Enabled, `radio_cli` or wiringPi missing | Fetch button and `scp` instructions |
| `reboot_required` | Block in `config.txt`, but card or `/dev/spidev0.0` absent | "Reboot now" via new token-guarded `POST /api/system/reboot` |
| `not_detected` | Overlay live, SPI probe fails. Probe on demand only, since it raises RSTB | "Board not found", all else unaffected |
| `ready` / `playing` | Probe succeeds | Panel |

- [ ] Applying the overlay: reuse the marked-block pattern in `scripts/wifi_onboard.sh:116-152`, but write
      `config.txt` as `.bak`, temp, `sync`, rename, `sync`, since it's on FAT.
- [ ] `radio:` section in `player.yaml` (`enabled: false`, `resume_on_boot: false`,
      `last_frequency_khz: 0`), written through `cfg_set`. Usual rules: `@app.on_event`, `_dump()`,
      explicit model defaults.
- [ ] `scripts/radio_setup.sh`: on explicit user action only. Show the licence, download from the vendor
      URL, check a pinned SHA-256, install to `/usr/local/lib/ugreen-dab+` (`rsync --delete` would remove it
      from the install dir). Document `scp` too, because the box often has no route out.

## Phase 4 — UI

- [ ] `frontend/src/components/RadioPanel.tsx`, with FM and DAB+ tabs, in `App.tsx` between `StreamBrowser`
      and `FilesBrowser`. Rendered only when `ready` or `playing`.
- [ ] **FM:** tune by kHz with step buttons, next/previous, RDS name and RadioText, RSSI and SNR meter,
      "no signal" state, presets in `config/radio_presets.json`. There is no documented FM band scan, so
      build one from seek or tune plus RSSI if the test shows that works.
- [ ] **DAB+:** background scan with progress (`-u`, up to about a minute), service list, DLS text (`-D -j`),
      signal quality, presets.
- [ ] Settings switch with the reboot state and a warning: don't hot-swap adapters or unplug while the
      board's LED is lit.
- [ ] `NowPlaying`: `radio` label and `force-media-title`, otherwise it shows the raw device URL. Hide
      bitrate and pause (the demuxer keeps reading, so unpausing would play stale audio). Add the label
      to `scripts/tui.py`.
- [ ] Show signal state beside the VU meter, because RF loss otherwise reads as quiet music.

## Phase 5 — soak the real option

- [ ] Long playback in FM, BT reconnects, a backend restart while listening, board removed, deploy
      keeps the settings.
- [ ] Compare hours per wedge across chip idle, chip on with no audio, and full radio. Check
      `get_throttled`.
- [ ] Compare SNR with polling on and off, ethernet versus WiFi dongle, antenna moved. 10 MHz SPI has
      harmonics at 90 and 100 MHz [inferred].
- [ ] CPU and RAM with viz on and off, against the 200 MB target.

## Phase 6 — docs and later

- [ ] `docs/radio.md`, and narrow the power-loss claim: SAFYDO stays safe, but the receiver is exposed while
      listening.
- [ ] Zero 2W port: same pins, but its Blinkt must come off, and it runs mpv 0.35.
