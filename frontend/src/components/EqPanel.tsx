import { SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { CollapseToggle } from "./CollapseToggle";
import { useCollapsed } from "../hooks/useCollapsed";
import { api } from "../api/client";

const PRESETS = ["flat", "speaker", "headphone", "custom"] as const;
const DEFAULT_FREQS = [60, 250, 1000, 4000, 12000];
const GAIN_MIN = -12;
const GAIN_MAX = 12;

function freqLabel(hz: number): string {
  return hz >= 1000 ? `${hz / 1000} kHz` : `${hz} Hz`;
}

function gainLabel(g: number): string {
  return `${g > 0 ? "+" : ""}${g} dB`;
}

interface Props {
  presetId: string;
  presetName?: string;
  enabled: boolean;
  eqApplied?: boolean;
  bands?: number[];
  bandFreqs?: number[];
  onChange: () => void;
}

export function EqPanel({
  presetId,
  presetName,
  enabled,
  eqApplied,
  bands,
  bandFreqs,
  onChange,
}: Props) {
  const freqs = bandFreqs?.length === 5 ? bandFreqs : DEFAULT_FREQS;
  const isCustom = presetId === "custom";
  const [collapsed, toggleCollapsed] = useCollapsed("eq");
  const [draft, setDraft] = useState<number[]>(bands ?? [0, 0, 0, 0, 0]);
  const [error, setError] = useState<string | null>(null);
  const lastEditRef = useRef(0);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined
  );

  // Adopt server bands unless the user just moved a slider — the 1 Hz
  // status refresh must not fight an in-progress drag.
  useEffect(() => {
    if (!bands?.length) return;
    if (Date.now() - lastEditRef.current < 1500) return;
    setDraft(bands.slice(0, 5));
  }, [bands]);

  useEffect(() => () => clearTimeout(saveTimerRef.current), []);

  // Shown, not left as unhandled rejections; the status is re-read either way.
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "EQ change failed");
    }
    onChange();
  };

  const toggle = () => act(() => api.eqEnabled(!enabled));

  const selectPreset = (id: string) =>
    act(async () => {
      await api.eqPreset(id);
      if (!enabled) await api.eqEnabled(true);
    });

  const editBand = (index: number, gain: number) => {
    lastEditRef.current = Date.now();
    const next = draft.map((g, i) => (i === index ? gain : g));
    setDraft(next);
    clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      api
        .eqCustom(next)
        .then(onChange)
        .catch(() => {
          /* next status refresh restores server state */
        });
    }, 350);
  };

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="h-4 w-4 text-mist" />
          <h2 className="font-display text-sm font-semibold">Sound EQ</h2>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            onClick={() => void toggle()}
            className={`relative h-7 w-12 rounded-full transition-colors ${
              enabled ? "bg-[var(--accent)]" : "bg-white/10"
            }`}
          >
            <span
              className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow transition-transform ${
                enabled ? "left-6" : "left-1"
              }`}
            />
          </button>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Sound EQ" />
        </div>
      </div>
      <p className="mt-2 text-xs text-mist">
        {enabled
          ? `5-band stereo · ${presetName ?? presetId}${eqApplied || isCustom ? "" : " (pending)"}`
          : "Off — flat passthrough"}
      </p>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {PRESETS.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => void selectPreset(id)}
            className={`rounded-lg px-3 py-1.5 text-xs capitalize transition-colors ${
              presetId === id
                ? "bg-[var(--accent)] text-white"
                : "bg-white/5 text-mist hover:bg-white/10"
            }`}
          >
            {id}
          </button>
        ))}
      </div>

      {isCustom && (
        <div className="mt-4 space-y-2">
          {freqs.map((hz, i) => (
            <label key={hz} className="flex items-center gap-2 text-xs">
              <span className="w-12 shrink-0 text-mist">{freqLabel(hz)}</span>
              <input
                type="range"
                min={GAIN_MIN}
                max={GAIN_MAX}
                step={1}
                value={draft[i] ?? 0}
                disabled={!enabled}
                onChange={(e) => editBand(i, Number(e.target.value))}
                className="h-1.5 min-w-0 flex-1 cursor-pointer appearance-none rounded-full bg-white/10 accent-[var(--accent)] disabled:opacity-40"
                aria-label={`${freqLabel(hz)} gain`}
              />
              <span className="w-12 shrink-0 text-right font-medium">
                {gainLabel(draft[i] ?? 0)}
              </span>
            </label>
          ))}
          <p className="text-[10px] text-mist">
            Saved automatically — kept after restart and preset switches.
          </p>
        </div>
      )}
    </section>
  );
}
