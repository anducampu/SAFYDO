import { Link2, Radio, Usb } from "lucide-react";
import { useState, type MutableRefObject, type ReactNode } from "react";
import { api } from "../api/client";
import { useRangeCommit } from "../hooks/useRangeCommit";
import { clock } from "../lib/clock";
import type { PlayerStatus, TrackInfo } from "../types";
import { genreAccent } from "../lib/genreColors";
import { splitGenres } from "../lib/genres";
import { Marquee } from "./Marquee";
import { RdsDetails } from "./RdsDetails";
import { VuMeter } from "./VuMeter";

interface Props {
  status: PlayerStatus | null;
  connected?: boolean;
  /** Live [left, right] channel levels from the WS spectrum frames. */
  bandsRef?: MutableRefObject<number[] | undefined>;
}

function title(status: PlayerStatus | null): string {
  if (!status) return "Safydo";
  // An unnamed preset or a station before its first RDS name gives "".
  if (status.radio?.listening) {
    return status.radio.name?.trim() || `${(status.radio.frequency_khz / 1000).toFixed(1)} FM`;
  }
  const info = status.player.mpv.track_info;
  const mpv = status.player.mpv;
  if (info?.title) return info.title;
  if (mpv.title) return mpv.title;
  const saved = status.player.saved;
  if (saved.meta?.name) return saved.meta.name;
  if (saved.meta?.filename) return saved.meta.filename;
  if (saved.path) return saved.path.split("/").pop() ?? "Local file";
  return "Ready";
}

/** Who is on air: RDS name on FM; for streams the stream's own name, else the list name. */
function stationName(status: PlayerStatus | null): string | null {
  if (!status) return null;
  if (status.radio?.listening) return status.radio.name?.trim() || null;
  const { mpv, saved } = status.player;
  if (mpv.idle) return null;
  if (saved.source === "stream") return mpv.track_info?.station?.trim() || saved.meta?.name || null;
  return saved.meta?.name || null;
}

function subtitle(status: PlayerStatus | null, info?: TrackInfo): string {
  if (!status) return "Select a station or USB track";
  const { mpv, saved } = status.player;
  if (status.radio?.listening) {
    if (status.radio.signal.valid === 0) return "No signal on this frequency";
    return status.radio.radio_text || "FM radio";
  }
  if (mpv.idle) return "Nothing playing";
  if (info?.description) return info.description;
  if (saved.source === "link") return saved.meta?.genre ?? "Web link";
  if (saved.source === "link_playlist") {
    const n = mpv.playlist_count ?? 0;
    const at = (mpv.playlist_pos ?? -1) + 1;
    return `Playlist · ${saved.meta?.name ?? ""}${n > 1 && at > 0 ? ` · ${at}/${n}` : ""}`;
  }
  if (saved.source === "stream") return saved.meta?.genre ?? "Live stream";
  if (saved.source === "usb") return "USB · local file";
  if (saved.source === "recording") return "Recording";
  return saved.source ?? "";
}

/**
 * Red through yellow to green. Hue runs 0-120 in a straight line, which passes
 * through yellow at the midpoint and clamps outside the scale.
 */
function signalColor(value: number, worst: number, best: number): string {
  const position = Math.min(1, Math.max(0, (value - worst) / (best - worst)));
  return `hsl(${Math.round(position * 120)} 85% 60%)`;
}

// FM reception scales: unusable at the low end, very good from the high end up.
const SNR_DB = { worst: 3, best: 30 };
const RSSI_DBUV = { worst: 10, best: 60 };
// Si468x guide (AN649): MULT has no unit. The chip's default FM_BLEND_MULTIPATH_LIMITS
// keep full stereo up to 45 and reach minimum separation at 60, so 60 is the red end.
const MULTIPATH = { worst: 60, best: 0 };

/** Seek bar for anything with a length (web links, local files). Status
 * arrives at 1 Hz, so the thumb steps once a second; while dragging, the
 * local value wins and the seek is sent on release. */
function SeekBar({ position, duration }: { position: number; duration: number }) {
  const [drag, setDrag] = useState<number | null>(null);
  const value = drag ?? Math.min(position, duration);
  const ref = useRangeCommit((target) => {
    setDrag(null);
    void api.seek(target).catch(() => {
      /* next status frame shows where playback really is */
    });
  });
  return (
    <div className="mt-3 flex items-center gap-2 text-[11px] tabular-nums text-mist">
      <span className="w-12 text-right">{clock(value)}</span>
      <input
        ref={ref}
        type="range"
        min={0}
        max={Math.floor(duration)}
        step={1}
        value={Math.floor(value)}
        onChange={(e) => setDrag(Number(e.target.value))}
        // A drag that ended where it began sends no change: stop freezing the thumb.
        onBlur={() => setDrag(null)}
        aria-label="Seek"
        className="h-1 min-w-0 flex-1 cursor-pointer accent-[var(--accent)]"
      />
      <span className="w-12">{clock(duration)}</span>
    </div>
  );
}

/** Live stream: scrub inside the cached rewind window; LIVE jumps back to
 * the edge. The left label is how far behind live you are. */
function LiveBar({
  position,
  start,
  end: cacheEnd,
  lead,
}: {
  position: number;
  start: number;
  end: number;
  /** Seconds mpv keeps buffered ahead even when "live". */
  lead: number;
}) {
  const [drag, setDrag] = useState<number | null>(null);
  const end = Math.max(start + 1, cacheEnd - lead);
  const span = Math.max(1, end - start);
  const value = drag ?? Math.min(Math.max(position, start), end);
  const behind = Math.max(0, end - value);
  const atLive = behind < 5;
  const ref = useRangeCommit((offset) => {
    setDrag(null);
    void api.seek(start + offset).catch(() => {});
  });
  return (
    <div className="mt-3 flex items-center gap-2 text-[11px] tabular-nums text-mist">
      <span className="w-12 text-right">{atLive ? "0:00" : `−${clock(behind)}`}</span>
      <input
        ref={ref}
        type="range"
        min={0}
        max={Math.floor(span)}
        step={1}
        value={Math.floor(value - start)}
        onChange={(e) => setDrag(start + Number(e.target.value))}
        onBlur={() => setDrag(null)}
        aria-label={`Rewind (${clock(span)} buffered)`}
        title={`${clock(span)} buffered`}
        className="h-1 min-w-0 flex-1 cursor-pointer accent-[var(--accent)]"
      />
      <button
        type="button"
        onClick={() => void api.goLive().catch(() => {})}
        disabled={atLive}
        title={atLive ? "Playing live" : "Back to live"}
        className={`flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider transition-colors ${
          atLive
            ? "bg-red-500/20 text-red-300"
            : "border border-white/15 text-mist hover:border-red-400/60 hover:text-white"
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${atLive ? "animate-pulse bg-red-400" : "bg-mist"}`} />
        Live
      </button>
    </div>
  );
}

function MetaChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-white/5 px-2.5 py-1 text-[11px] text-mist">
      {children}
    </span>
  );
}

export function NowPlaying({ status, bandsRef }: Props) {
  const mpv = status?.player.mpv;
  const info = mpv?.track_info;
  const playing = Boolean(mpv?.running && !mpv?.idle);
  const savedGenre = status?.player.saved.meta?.genre;
  const genres = info?.genres?.length ? info.genres : splitGenres(savedGenre);
  const source = status?.player.saved.source;
  const spectrum = status?.spectrum;
  const radio = status?.radio?.listening ? status.radio : null;
  const station = stationName(status);

  const formatLabel = info?.format
    ? info.sample_rate
      ? `${info.format} · ${Math.round(info.sample_rate / 1000)} kHz`
      : info.format
    : info?.sample_rate
      ? `${Math.round(info.sample_rate / 1000)} kHz`
      : null;

  return (
    <section className="glass accent-ring rounded-2xl p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="flex min-w-0 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <span className="shrink-0">Now playing{station ? ":\u00a0" : ""}</span>
            {station && (
              <span
                className="truncate font-semibold text-white"
                style={{ textShadow: "0 0 10px var(--accent), 0 0 2px var(--accent)" }}
                title={station}
              >
                {station}
              </span>
            )}
          </p>
          <Marquee
            text={title(status)}
            className="font-display mt-1 text-2xl font-semibold leading-tight sm:text-3xl"
          />
          <Marquee text={subtitle(status, info)} className="mt-1 text-sm text-mist" />
        </div>
        <div
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full"
          style={{ background: "color-mix(in srgb, var(--accent) 25%, transparent)" }}
        >
          {source === "usb" ? (
            <Usb className="h-5 w-5" style={{ color: "var(--accent)" }} />
          ) : source === "link" || source === "link_playlist" ? (
            <Link2 className="h-5 w-5" style={{ color: "var(--accent)" }} />
          ) : (
            <Radio className="h-5 w-5" style={{ color: "var(--accent)" }} />
          )}
        </div>
      </div>

      <VuMeter levels={spectrum?.bands} levelsRef={bandsRef} />

      {!radio && playing && status?.player.live && (mpv?.cache_ranges?.length ?? 0) > 0 ? (
        <LiveBar
          position={mpv?.position ?? 0}
          start={mpv!.cache_ranges![0][0]}
          end={mpv!.cache_ranges![mpv!.cache_ranges!.length - 1][1]}
          lead={status?.player.live_lead_s ?? 10}
        />
      ) : (
        !radio &&
        playing &&
        !status?.player.live &&
        mpv?.duration != null &&
        mpv.duration > 0 && <SeekBar position={mpv.position ?? 0} duration={mpv.duration} />
      )}

      {radio && (
        <div className="mt-3 flex flex-wrap gap-2">
          <MetaChip>FM · {(radio.frequency_khz / 1000).toFixed(1)} MHz</MetaChip>
          {radio.signal.snr != null && (
            <MetaChip>
              SNR{" "}
              <span
                className="font-medium"
                style={{ color: signalColor(radio.signal.snr, SNR_DB.worst, SNR_DB.best) }}
              >
                {radio.signal.snr} dB
              </span>
            </MetaChip>
          )}
          {radio.signal.rssi != null && (
            <MetaChip>
              RSSI{" "}
              <span
                className="font-medium"
                style={{ color: signalColor(radio.signal.rssi, RSSI_DBUV.worst, RSSI_DBUV.best) }}
              >
                {radio.signal.rssi} dBµV
              </span>
            </MetaChip>
          )}
          {radio.signal.multipath != null && (
            <MetaChip>
              Multipath{" "}
              <span
                className="font-medium"
                style={{ color: signalColor(radio.signal.multipath, MULTIPATH.worst, MULTIPATH.best) }}
              >
                {radio.signal.multipath}
              </span>
            </MetaChip>
          )}
          {radio.mono && <MetaChip>Listening in mono</MetaChip>}
        </div>
      )}
      {radio?.rds && <RdsDetails rds={radio.rds} frequencyKhz={radio.frequency_khz} />}

      {!radio && playing && (info?.bitrate_kbps || formatLabel || genres.length || info?.listeners != null) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {info?.bitrate_kbps != null && (
            <MetaChip>{info.bitrate_kbps} kbps</MetaChip>
          )}
          {formatLabel && <MetaChip>{formatLabel}</MetaChip>}
          {genres.map((g) => (
            <span
              key={g}
              className="rounded-full px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide"
              style={{ background: `${genreAccent(g)}26`, color: genreAccent(g) }}
            >
              {g}
            </span>
          ))}
          {info?.listeners != null && (
            <MetaChip>{info.listeners.toLocaleString()} listeners</MetaChip>
          )}
        </div>
      )}
    </section>
  );
}