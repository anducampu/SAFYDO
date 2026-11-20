import {
  ChevronRight,
  Minus,
  Play,
  Plus,
  Radio,
  Save,
  Search,
  SkipBack,
  SkipForward,
  Square,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CollapseToggle } from "./CollapseToggle";
import { useCollapsed } from "../hooks/useCollapsed";
import { api } from "../api/client";
import type { RadioStatus } from "../types";

interface Props {
  onChange: () => void;
}

const mhz = (khz: number) => (khz / 1000).toFixed(1);

/** SNR is what decides how it sounds, so it drives the bars. */
function SignalBars({ snr }: { snr?: number }) {
  const bars = snr == null ? 0 : snr >= 25 ? 4 : snr >= 15 ? 3 : snr >= 8 ? 2 : snr >= 3 ? 1 : 0;
  return (
    <span className="flex items-end gap-0.5" aria-hidden>
      {[1, 2, 3, 4].map((n) => (
        <span
          key={n}
          className={`w-1 rounded-sm ${n <= bars ? "bg-[var(--accent)]" : "bg-white/15"}`}
          style={{ height: `${n * 3 + 3}px` }}
        />
      ))}
    </span>
  );
}

function IconButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-white/5 text-mist transition-colors hover:text-white disabled:opacity-40"
    >
      {children}
    </button>
  );
}

export function RadioPanel({ onChange }: Props) {
  const [radio, setRadio] = useState<RadioStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [entry, setEntry] = useState("");
  const [collapsed, toggleCollapsed] = useCollapsed("radio");

  // A 2.5 s poll sent before a tune/stop can answer after it: drop any answer
  // older than the newest request, or the old frequency flickers back.
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    api
      .radio()
      .then((r) => mine === seq.current && setRadio(r))
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 2500);
    return () => clearInterval(timer);
  }, [load]);

  const run = async (action: () => Promise<RadioStatus>) => {
    const mine = ++seq.current;
    setBusy(true);
    setError(null);
    try {
      const r = await action();
      if (mine === seq.current) setRadio(r);
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Radio error");
    } finally {
      setBusy(false);
    }
  };

  if (!radio) return null;

  const listening = radio.listening;
  const khz = radio.frequency_khz;
  const band = radio.band;
  const scanning = radio.scan.running;
  const locked = busy || scanning;
  const signal = radio.signal;
  const noSignal = listening && signal.valid === 0;
  const isPreset = radio.presets.some((p) => p.frequency_khz === khz);

  const go = (target: number) => {
    const clamped = Math.min(band.max_khz, Math.max(band.min_khz, target));
    void run(() => (listening ? api.radioTune(clamped) : api.radioPlay(clamped)));
  };

  const submitEntry = (e: React.FormEvent) => {
    e.preventDefault();
    const value = Number.parseFloat(entry.replace(",", "."));
    if (Number.isNaN(value)) return;
    setEntry("");
    go(Math.round(value * 10) * 100);
  };

  const savePreset = () => {
    const name = prompt(`Name for ${mhz(khz)} FM`, radio.rds?.ps || radio.radio_text.slice(0, 40));
    if (name === null) return;
    void run(() => api.radioAddPreset(name, khz));
  };

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-display text-lg font-semibold">
          <Radio className="h-4 w-4 text-[var(--accent)]" /> FM radio
        </h2>
        <div className="flex items-center gap-2">
          {listening && (
            <span className="flex items-center gap-2 text-xs text-mist">
              {signal.snr != null && <span>SNR {signal.snr} dB</span>}
              <SignalBars snr={signal.snr} />
            </span>
          )}
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="FM radio" />
        </div>
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="flex gap-1.5">
          <IconButton label="Previous station" onClick={() => void run(() => api.radioSeek(false))} disabled={locked || !listening}>
            <SkipBack className="h-4 w-4" />
          </IconButton>
          <IconButton label="Down 0.1 MHz" onClick={() => go(khz - band.step_khz)} disabled={locked}>
            <Minus className="h-4 w-4" />
          </IconButton>
        </div>
        <p className="min-w-0 text-center font-display text-4xl font-semibold tabular-nums sm:text-5xl">
          {mhz(khz)}
          <span className="ml-1.5 text-sm font-normal text-mist">MHz</span>
        </p>
        <div className="flex gap-1.5">
          <IconButton label="Up 0.1 MHz" onClick={() => go(khz + band.step_khz)} disabled={locked}>
            <Plus className="h-4 w-4" />
          </IconButton>
          <IconButton label="Next station" onClick={() => void run(() => api.radioSeek(true))} disabled={locked || !listening}>
            <SkipForward className="h-4 w-4" />
          </IconButton>
        </div>
      </div>

      <p className="mt-2 min-h-5 truncate text-center text-sm text-mist">
        {noSignal ? "No signal on this frequency" : radio.radio_text || (listening ? "" : "Tuner off")}
      </p>

      <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
        <button
          type="button"
          disabled={locked}
          onClick={() => void run(listening ? api.radioStop : () => api.radioPlay(khz))}
          className="flex h-10 items-center gap-2 rounded-full bg-[var(--accent)] px-5 text-sm font-medium text-white disabled:opacity-40"
        >
          {listening ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {listening ? "Stop" : "Listen"}
        </button>
        <button
          type="button"
          disabled={locked || !listening}
          aria-pressed={radio.mono}
          onClick={() => void run(() => api.radioMono(!radio.mono))}
          title="Sums left and right, which removes most of the hiss on a weak stereo station. Remembered per frequency."
          className={`flex h-10 items-center gap-2 rounded-full px-4 text-sm disabled:opacity-40 ${
            radio.mono ? "bg-[var(--accent)] text-white" : "bg-white/5 text-mist hover:text-white"
          }`}
        >
          Mono
        </button>
        <button
          type="button"
          disabled={locked || !listening || isPreset}
          onClick={savePreset}
          className="flex h-10 items-center gap-2 rounded-full bg-white/5 px-4 text-sm text-mist hover:text-white disabled:opacity-40"
        >
          <Save className="h-4 w-4" /> Save
        </button>
        <button
          type="button"
          disabled={locked || !listening}
          onClick={() => void run(api.radioScan)}
          title="Mutes the radio while it steps through the band"
          className="flex h-10 items-center gap-2 rounded-full bg-white/5 px-4 text-sm text-mist hover:text-white disabled:opacity-40"
        >
          <Search className="h-4 w-4" />
          {scanning ? `Scanning ${radio.scan.progress}%` : "Scan"}
        </button>
        <form onSubmit={submitEntry} className="flex">
          <input
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
            inputMode="decimal"
            placeholder="93.5"
            aria-label="Frequency in MHz"
            disabled={locked}
            className="h-10 w-20 rounded-l-full bg-black/20 pl-4 text-sm outline-none placeholder:text-mist/60"
          />
          <button
            type="submit"
            disabled={locked || !entry}
            aria-label="Tune"
            className="flex h-10 w-9 items-center justify-center rounded-r-full bg-white/5 text-mist hover:text-white disabled:opacity-40"
          >
            <ChevronRight className="h-4 w-4" />
          </button>
        </form>
      </div>

      {radio.presets.length > 0 && (
        <div className="mt-4">
          <p className="mb-1.5 text-xs uppercase tracking-wide text-mist">Presets</p>
          <div className="flex flex-wrap gap-1.5">
            {radio.presets.map((p) => (
              <span
                key={p.frequency_khz}
                className={`flex items-center rounded-full ${
                  p.frequency_khz === khz && listening ? "bg-[var(--accent)] text-white" : "bg-white/5"
                }`}
              >
                <button
                  type="button"
                  disabled={locked}
                  onClick={() => go(p.frequency_khz)}
                  className="py-1.5 pl-3 pr-1 text-xs font-medium disabled:opacity-40"
                >
                  {p.name} <span className="opacity-60">{mhz(p.frequency_khz)}</span>
                </button>
                <button
                  type="button"
                  disabled={locked}
                  onClick={() => void run(() => api.radioDeletePreset(p.frequency_khz))}
                  aria-label={`Remove ${p.name}`}
                  className="py-1.5 pl-1 pr-2 opacity-60 hover:opacity-100 disabled:opacity-40"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        </div>
      )}

      {radio.stations.length > 0 && (
        <div className="mt-4">
          <p className="mb-1.5 text-xs uppercase tracking-wide text-mist">
            Found {radio.stations.length} {radio.stations.length === 1 ? "station" : "stations"}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {radio.stations.map((s) => (
              <button
                key={s.frequency_khz}
                type="button"
                disabled={locked}
                onClick={() => go(s.frequency_khz)}
                title={`SNR ${s.snr} dB`}
                className={`rounded-full px-3 py-1.5 text-xs tabular-nums disabled:opacity-40 ${
                  s.frequency_khz === khz && listening ? "bg-[var(--accent)] text-white" : "bg-white/5 text-mist hover:text-white"
                }`}
              >
                {mhz(s.frequency_khz)}
              </button>
            ))}
          </div>
        </div>
      )}

      {(error || radio.error) && (
        <p className="mt-3 text-xs text-red-300">{error || radio.error}</p>
      )}
    </section>
  );
}
