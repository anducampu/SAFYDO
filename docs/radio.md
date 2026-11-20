# FM radio (DABBoard)

Optional. A [uGreen DABBoard](https://ugreen.eu/product/ugreen-dab-board/) on the Pi 3 A+ header plays FM on
your Bluetooth speaker like any other source, with EQ, VU meter and volume, from the **FM radio** panel in the
web UI. It is off by default.

**DAB+ is not offered yet.** The chip supports it, but it has not been tested against a real ensemble.

## Fitting and setup

1. Fit the board with the power off, and connect a passive 50 Ω antenna to the SMA socket. The board has no
   EEPROM, so nothing proves it is there until the tuner answers.
2. Install the vendor tools. Their licence forbids redistribution, so SAFYDO doesn't ship them:

   ```
   sudo /opt/rpi-audio-player/scripts/radio_setup.sh               # shows the licence
   sudo /opt/rpi-audio-player/scripts/radio_setup.sh --accept-licence
   ```

   Without a route out, copy the zip and the WiringPi `.deb` over and pass `--zip` and `--wiringpi-deb`.
3. **Settings → FM radio**, switch it on, then **Reboot now**.

GPIO 23 is both the tuner's reset and the Blinkt data line, so the two can't share a board. Neither can be detected,
so SAFYDO goes by what you tell it: the LED switch only appears with **Settings → LED meter → Blinkt fitted** on
(off by default on the 3 A+), the radio won't turn on while that's on, and enabling one while the other is on is
refused. Don't turn on "Blinkt fitted" with a DABBoard on the header: the LEDs would clock its reset line.

## Power loss

SAFYDO is built to survive the plug being pulled. **The tuner is the exception.** While it is out of reset, the
vendor warns that voltage fluctuations can permanently damage the chip. The board's LED shows that state.

To limit the exposure, the tuner runs only while you listen: Stop, any other source and a clean shutdown all put
it back in reset, as does giving up after repeated playback failures. It won't start while the Pi reports under-voltage. It also won't start by itself after a
boot unless you turn on **Start radio after a reboot**. While listening, shut down from the UI before unplugging,
and don't swap USB adapters.

## RDS and RBDS

Now Playing shows everything the station sends, decoded from the raw RDS groups in `backend/services/rds.py`:
station name (PS), station code (PI) with its country code, coverage and programme reference, the alternative
frequencies (AF) with LF/MF entries, programme type and PTY name, TP, TA, music or speech, stereo and the other
decoder flags, radio text, RadioText+ tags, station clock, extended country code, language, programme item number,
other stations on the network (EON) and open data applications.

A field appears only once its data has arrived, and many stations send only part of this. The clock is what the
station broadcasts, right or wrong. Country and language are shown as the codes received, not as names. Choose
RBDS in Settings for North America: it changes the programme type names and adds the call sign worked out from the
PI code. RDS is sampled at 4 groups a second for the first 90 seconds on a frequency, then once a second, so a long
AF list can take a minute or two to fill.

**Reading the signal chips.** SNR and RSSI run from red to green on a reception scale chosen for this project.
Multipath is the chip's raw `MULT` value, range 0-255 and no unit (Si468x guide, AN649). Red is 60, because the
chip's default `FM_BLEND_MULTIPATH_LIMITS` keep full stereo up to 45 and reach minimum separation at 60.

**Mono.** The **Mono** button in the panel sums left and right, which cancels most of the hiss on a weak stereo
station, since FM's noise sits mainly in the difference signal. It's remembered per frequency.

## How audio flows

```
Si4688 --I2S--> arecord --FIFO (raw S16 48 kHz)--> mpv --> Bluetooth
```

- `arecord` and not mpv's `av://alsa`: ffmpeg's ALSA reader busy-polls, pinning a core at 100 %. On the 3 A+
  that brought down the Bluetooth controller within a minute, three times in four tries.
- `S16_LE` only. The board fails `S32_LE` with `-EIO` and delivers nothing above the low 8 bits in `S24_LE`.
- The cache is capped low. Raw PCM arrives in small packets costing about three times their size.
- Samples are grouped into 50 ms frames ahead of the VU branch. Without that, the branch falls behind and frames
  pile up in the filter graph at about 3.5 MB/min while a web page is open.

Control goes through the vendor's one-shot `radio_cli`, which leaves the chip running when it exits.

## API

| Call | Purpose |
|---|---|
| `GET /api/radio` | State, frequency, signal, radio text, presets, scan results |
| `POST /api/radio/play` `{frequency_khz}` | Start listening |
| `POST /api/radio/tune` `{frequency_khz}` | Change frequency |
| `POST /api/radio/seek` `{up}` | Next or previous station |
| `POST /api/radio/scan` | Step through the band, muted, about two minutes |
| `POST /api/radio/stop` | Stop and put the tuner in reset |
| `POST` / `DELETE /api/radio/presets` | Manage presets |

## Troubleshooting

| Symptom | Check |
|---|---|
| Panel missing | Settings → FM radio shows the state: tools missing, reboot needed, or board not found |
| "The DABBoard did not answer" | Board seated on the header, then `sudo radio_cli -b F -o 1 -F <kHz>` |
| No signal | Antenna fitted. Wire "window" DAB antennas don't work for FM |
| Audio stops and comes back | The speaker dropped: the radio restarts by itself when it returns, and the tuner goes into reset after 5 minutes without one |
