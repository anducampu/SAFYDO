import { Loader2, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { StreamSettings, StreamSettingsResponse } from "../types";

/** Settings -> Streams: the mpv counterpart of StreamJockey's stream sheet.
 * Saved values are applied to the running player at once (no restart). */

type NumKey = Exclude<keyof StreamSettings, "prebuffer_on_start" | "user_agent">;

const FIELDS: { key: NumKey; label: string; hint: string; min: number; max: number; step: number; unit: string }[] = [
  {
    key: "cache_secs",
    label: "Read-ahead cache",
    hint: "How far ahead the player buffers. Also caps RAM on long web links.",
    min: 10,
    max: 300,
    step: 5,
    unit: "s",
  },
  {
    key: "rebuffer_secs",
    label: "Refill after a stall",
    hint: "Seconds of audio to gather before playing again after the network hiccups.",
    min: 0.5,
    max: 30,
    step: 0.5,
    unit: "s",
  },
  {
    key: "timeout_secs",
    label: "Connect / read timeout",
    hint: "How long a silent server gets before it counts as dropped.",
    min: 3,
    max: 120,
    step: 1,
    unit: "s",
  },
  {
    key: "reconnect_delay_max",
    label: "Longest wait between reconnects",
    hint: "Reconnects back off up to this, then give up.",
    min: 1,
    max: 120,
    step: 1,
    unit: "s",
  },
  {
    key: "output_buffer",
    label: "Bluetooth output buffer",
    hint: "Below 0.5 s, bluealsa drops audio under load. Applies from the next track.",
    min: 0.2,
    max: 2,
    step: 0.1,
    unit: "s",
  },
];

export function StreamSettingsForm({ row }: { row: string }) {
  const [data, setData] = useState<StreamSettingsResponse | null>(null);
  const [draft, setDraft] = useState<StreamSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api
      .streamSettings()
      .then((d) => {
        setData(d);
        setDraft(d);
      })
      .catch((e) => setMsg(e instanceof Error ? e.message : "Unavailable"));
  }, []);

  if (!draft || !data) return <p className="text-xs text-mist">{msg ?? "Loading…"}</p>;

  const set = <K extends keyof StreamSettings>(key: K, value: StreamSettings[K]) =>
    setDraft((d) => (d ? { ...d, [key]: value } : d));

  const dirty = (Object.keys(draft) as (keyof StreamSettings)[]).some((k) => draft[k] !== data[k]);
  const unlimited = draft.retries === 0;

  const save = async (reset = false) => {
    setBusy(true);
    setMsg(null);
    try {
      const d = await api.putStreamSettings(reset ? { reset: true } : draft);
      setData(d);
      setDraft(d);
      setMsg(reset ? "Defaults restored and applied." : "Saved and applied — no restart needed.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <section>
        <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Reconnect</h3>
        <div className={row}>
          <div>
            <p className="font-medium">Unlimited retries</p>
            <p className="text-xs text-mist">
              {data.retries_supported
                ? "Keep reconnecting a dropped stream for as long as it takes."
                : "This board's ffmpeg is older than 7, so a retry limit cannot be enforced: retries are always unlimited."}
            </p>
          </div>
          <input
            type="checkbox"
            checked={unlimited || !data.retries_supported}
            disabled={!data.retries_supported}
            onChange={(e) => set("retries", e.target.checked ? 0 : 5)}
            className="h-4 w-4 accent-[var(--accent)]"
          />
        </div>
        {!unlimited && data.retries_supported && (
          <label className={row}>
            <span className="font-medium">Max retries</span>
            <input
              type="number"
              min={1}
              max={99}
              value={draft.retries}
              onChange={(e) => set("retries", Math.max(1, Math.min(99, Number(e.target.value) || 1)))}
              className="w-20 rounded-lg bg-black/30 px-2 py-1.5 text-right text-xs"
            />
          </label>
        )}
        <div className={row}>
          <div>
            <p className="font-medium">Buffer before the first sound</p>
            <p className="text-xs text-mist">Wait for the refill amount at start too, not only after a stall.</p>
          </div>
          <input
            type="checkbox"
            checked={draft.prebuffer_on_start}
            onChange={(e) => set("prebuffer_on_start", e.target.checked)}
            className="h-4 w-4 accent-[var(--accent)]"
          />
        </div>
      </section>

      <section>
        <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Buffering &amp; timeouts</h3>
        {FIELDS.map((f) => (
          <label key={f.key} className={row}>
            <span className="min-w-0">
              <span className="block font-medium">{f.label}</span>
              <span className="block text-xs text-mist">
                {f.hint} Default {data.defaults[f.key]} {f.unit}.
              </span>
            </span>
            <span className="flex shrink-0 items-center gap-1">
              <input
                type="number"
                min={f.min}
                max={f.max}
                step={f.step}
                value={draft[f.key]}
                onChange={(e) => set(f.key, Number(e.target.value))}
                className="w-20 rounded-lg bg-black/30 px-2 py-1.5 text-right text-xs"
              />
              <span className="w-3 text-xs text-mist">{f.unit}</span>
            </span>
          </label>
        ))}
      </section>

      <section className="lg:col-span-2">
        <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Identity</h3>
        <label className="flex items-center gap-3 py-2">
          <span className="w-24 shrink-0 font-medium">User agent</span>
          <input
            value={draft.user_agent}
            maxLength={200}
            placeholder="mpv default (libmpv)"
            onChange={(e) => set("user_agent", e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-black/20 px-3 py-1.5 text-xs outline-none focus:border-[var(--accent)]"
          />
        </label>
        <p className="text-xs text-mist">
          Some stations refuse unknown players; a browser-like value can help. Timeout, user agent and reconnect apply
          to the next station you start.
        </p>
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            disabled={busy || !dirty}
            onClick={() => void save()}
            className="flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Save
          </button>
          <button
            type="button"
            disabled={busy || !dirty}
            onClick={() => setDraft(data)}
            className="rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:text-white disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void save(true)}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:text-white disabled:opacity-40"
          >
            <RotateCcw className="h-3.5 w-3.5" /> Defaults
          </button>
        </div>
        {msg && <p className="mt-2 text-xs text-[var(--accent)]">{msg}</p>}
      </section>
    </>
  );
}
