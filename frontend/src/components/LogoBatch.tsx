import { ImageDown, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { LogoBatchStatus } from "../types";

/**
 * Settings → Sources: look up logos for the whole station list in the
 * background. The pass runs on the board, so closing Settings doesn't stop it.
 */
export function LogoBatch({ row }: { row: string }) {
  const [st, setSt] = useState<LogoBatchStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wasRunning = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const s = await api.logoBatch();
        if (cancelled) return;
        // The pass just ended: let the station list pick up the new logos.
        // Kept out of the setSt updater, which StrictMode runs twice.
        if (wasRunning.current && !s.running) window.dispatchEvent(new Event("safydo:streams-changed"));
        wasRunning.current = s.running;
        setSt(s);
        timer = window.setTimeout(poll, s.running ? 1500 : 10000);
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, 10000);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  const start = async (onlyMissing: boolean) => {
    setError(null);
    try {
      const s = await api.startLogoBatch(onlyMissing);
      wasRunning.current = s.running;
      setSt(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start");
    }
  };

  const running = Boolean(st?.running);
  const finished = st && !running && st.total > 0;

  return (
    <div className={row}>
      <div className="min-w-0">
        <p className="font-medium">Station logos</p>
        <p className="text-xs text-mist">
          {running
            ? `Searching ${st!.done}/${st!.total}${st!.current ? ` · ${st!.current}` : ""} · ${st!.found} found`
            : finished
              ? `Last pass: ${st!.found} of ${st!.total} found`
              : "From each station's website and the Radio Browser directory"}
        </p>
        {finished && st!.missing.length > 0 && (
          <p className="mt-0.5 line-clamp-2 text-[10px] text-mist" title={st!.missing.join(", ")}>
            None for: {st!.missing.join(", ")}
          </p>
        )}
        {error && <p className="text-xs text-red-300">{error}</p>}
      </div>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          disabled={running}
          onClick={() => void start(true)}
          title="Only stations that have no logo yet"
          className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ImageDown className="h-3.5 w-3.5" />} Find missing
        </button>
        <button
          type="button"
          disabled={running}
          onClick={() => void start(false)}
          title="Search again for every station, replacing the logos it finds"
          className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
        >
          <RefreshCw className="h-3.5 w-3.5" /> All
        </button>
      </div>
    </div>
  );
}
