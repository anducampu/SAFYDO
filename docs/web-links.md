# Web links (YouTube, SoundCloud, Mixcloud, Vimeo, Dailymotion, Rumble)

The **Web links** panel keeps a list of pages from these six sites and plays
their audio through the same mpv → bluealsa path as every other source.

## How it works

- **Adding** a link: `POST /api/links {url}` runs `yt-dlp -J --flat-playlist`
  once to read title, uploader, duration, thumbnail (and the entry count for a
  playlist). The list is `/var/lib/audio-player/links.json` (state dir, so a
  deploy never overwrites it; written atomically).
- **Playing**: `POST /api/play/link/{id}` loads `ytdl://<url>` into mpv. The
  prefix sends it straight to mpv's `ytdl_hook` instead of letting mpv try the
  HTML page as media first. `mpv_daemon.sh` passes `--ytdl-format=bestaudio/best`
  (audio only) and pins `/usr/local/bin/yt-dlp`.
- **Seeking**: `POST /api/transport/seek {position}`; the Now Playing panel shows
  a seek bar for anything with a duration (links and local files).
- Thumbnails are loaded by the viewer's browser, not by the Pi.
- Only the six sites are accepted (host match in `backend/services/links.py`).

## Playlists

Web links → **Playlists**: any number of playlists, any number of links each
(`/var/lib/audio-player/link_playlists.json`; a link appears once per
playlist, deleting a link removes it from every playlist).

- **Order**: ↑/↓ per entry; tap an entry to play from there. Add by pasting a
  new URL (it joins the library too) or from "From your links".
- **Play**: `POST /api/play/link-playlist/{id}?start=N` loads the whole list
  into mpv's own playlist — the chosen entry first, the rest queued around it
  with `loadfile … insert-at|append` — so mpv resolves each entry through
  ytdl_hook only when it becomes current. Each entry carries its own per-file
  options (Vimeo cookies, Rumble impersonation), so platforms mix freely.
- **Shuffle** = mpv `playlist-shuffle` (the current entry keeps playing).
  **Loop** = `loop-playlist=inf`. Single plays reset `loop-playlist=no`.
- **Edits apply live** to the playing playlist (`PlayerService.sync_playlist`):
  removed entries leave mpv's queue (`playlist-remove`), new ones are appended
  with their per-file options, then `playlist-move` puts everything in the
  saved order — the playing entry is never reloaded. Shuffled: membership
  only. Shuffle off re-syncs to the saved order instead of
  `playlist-unshuffle`, which would restore mpv's pre-shuffle order and lose
  edits made meanwhile. An entry removed while it plays finishes, parked past
  the end. Verified on the 3 A+ (reorder, remove+add, edit while shuffled,
  shuffle off, next) with no break in the playing track.
- **Prev/Next** (`POST /api/transport/prev|next`) show in Playback whenever
  mpv's queue has more than one entry — also for a YouTube/SoundCloud
  playlist URL.

Verified on the 3 A+ (YouTube → Vimeo → Rumble): auto-advance, next, prev,
loop and shuffle toggled live.

## Install

`install.sh` → `install_ytdlp()`:

- builds a venv at `/opt/yt-dlp` with `yt-dlp[default,curl-cffi]` and links it
  as `/usr/local/bin/yt-dlp` (the Debian package falls behind YouTube within
  weeks); if that fails, the official zipapp is the fallback (no Rumble);
- `audio-player-ytdlp-update.timer` runs `scripts/ytdlp_update.sh` weekly
  (pip upgrade in the venv, or `yt-dlp -U` for the zipapp);
- installs `nodejs` and adds `--js-runtimes node` to `/etc/yt-dlp.conf` —
  YouTube needs a JS runtime for its player challenge, yt-dlp wants Node 20+.
  Trixie has 20.19; **Bookworm has 18.x, so YouTube may fail on the Zero 2W**
  while the other five platforms work.
- `YTDLP=0 ./install.sh` skips all of it.

## Quality

Sources are lossy (YouTube Opus ~130–160 kbps, SoundCloud AAC/MP3 128–256,
Mixcloud AAC). That is below what the BT link carries (SBC-XQ 570 kbps), so the
link adds no further loss beyond the codec re-encode every source already gets.
Vimeo/Dailymotion/Rumble often offer only muxed video+audio; mpv plays the
audio and drops the video, but the video is still downloaded.

## Measured on the 3 A+ (2026-11-17, yt-dlp 2026.10.26, no Node.js)

| Platform | Add (probe) | Play → audio | Format |
|---|---|---|---|
| YouTube | 12.1 s | ~6–7 s | Opus ~100 kbps / 48 kHz |
| SoundCloud | 13.3 s | — | — |
| Mixcloud | 10.1 s | 12 s | AAC ~68 kbps |
| Dailymotion | 10.3 s | — | — |
| Vimeo | needs account cookies | plays (user-verified 2026-11-17) | anonymous: "web client only works when logged-in" |
| Rumble | 10.5 s | ~13 s | AAC 192 kbps (needs impersonation, see below) |

- YouTube played **without** a JS runtime for this video; Node stays in
  install.sh in case YouTube tightens the challenge.
- Seek on a 62-min Mixcloud mix landed at 1802.9 s for a 1800 s request.
- RAM: 202–205 MB used (whole box) while playing a link, vs 176 MB idle earlier the same day.

## Account cookies (Vimeo)

Vimeo refuses anonymous playback ("The web client only works when
logged-in"). Web links → **Accounts** takes a Netscape `cookies.txt` per
platform, exported from a browser where you are signed in.

- Stored at `/var/lib/audio-player/cookies/<platform>.txt`, mode 0600, root.
  The API only ever says whether a file exists and when it was saved.
- Validated on upload: must parse as cookies.txt. Only that platform's
  domains are kept — a browser export holds every site's cookies, and those
  never reach the Pi's disk.
- Each use copies it to `/run/audio-player/cookies/` (tmpfs) and points yt-dlp
  there: yt-dlp rewrites its jar on exit, so the upload stays pristine and
  plays cost no SD writes. Probe: `--cookies`; mpv: per-file option
  `ytdl-raw-options-append=cookies=…` on `loadfile`, so it applies to that
  link only.
- Works until the browser session expires or you log out there.

## Rumble

Rumble's embed JSON API answers 403 to yt-dlp's plain HTTP client (TLS
fingerprinting); the page itself loads. Fixed with browser impersonation:
yt-dlp lives in a venv at `/opt/yt-dlp` with `curl_cffi` (armv7l wheel), and
Rumble links get `--impersonate chrome` (probe) / `ytdl-raw-options=impersonate=chrome`
(mpv, per file). `IMPERSONATE` in `links.py` lists the sites that get it.
Verified on the 3 A+: added in 10.5 s, AAC 192 kbps after ~13 s.

## Live rewind (all live streams)

Setting **Live rewind** (Settings → General, `audio.rewind_minutes`, default
10, 0–45) sizes mpv's back buffer (`demuxer-max-back-bytes`, RAM), capped at
96 MB (raised from 64 MB so 30 min fits at the measured ~320 kbps). Now Playing shows a bar over the cached window and a **LIVE** button
(`POST /api/transport/live`). Live = 10 s behind the newest cached byte
(`LIVE_LEAD_S`) — jumping to the very end leaves no jitter buffer. Every
station counts as live (mpv's `duration` on a live HTTP stream is the cache
length, so it cannot tell); a web link is live when yt-dlp says `is_live`.

**Sizing uses the cache's real cost, not the stream bitrate.** mpv counts
its byte limits with per-packet overhead (struct + padding); an MP3 stream
is ~38 packets/s, so a station declaring 168 kbps costs **~320 kbps** in the
cache (`total-bytes` / seconds held, 2026-11-17). Sizing from the declared
bitrate gave about half the window asked for. `apply_rewind()` runs from
`status()` at 1 Hz: declared rate +10 % until 20 s are cached, then the
measured rate; the highest seen per file wins; reset when the file changes
(playlist advance); mpv only touched on a >5 % change. Settings shows the
budget, the rate and — when the cap bites — the minutes it really holds
(at the old 64 MB cap: 30 min asked → 27.8 min at 322 kbps). At 96 MB, user-verified: 30 min → 68.3 MB at 318 kbps
(measured), the full window, no cap message.

## Open

- Zero 2W (Bookworm, Node 18): resolve times and whether YouTube still plays.
- Live rewind on a YouTube live (HLS) link: not yet verified.
