import { Activity, Check, Eye, EyeOff, Loader2, Play, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { ProbeCandidate, StreamCheckResult, StreamCheckStatus } from "../types";
import { DeleteButton } from "./DeleteButton";

const btn =
  "flex items-center gap-1 rounded-md bg-white/5 px-2 py-1 text-[11px] text-mist hover:bg-white/10 hover:text-white disabled:opacity-50";

/**
 * Settings → Sources: open every station once and list the ones that don't
 * answer like audio, each with something to do about it. The check runs on
 * the board, so closing Settings doesn't stop it.
 */
export function StreamCheck({ row }: { row: string }) {
  const [st, setSt] = useState<StreamCheckStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  // An answer landing after Settings closed used to schedule the next poll
  // anyway, and a reopen started a second chain beside it.
  const mounted = useRef(false);

  const schedule = () => {
    window.clearTimeout(timer.current);
    if (mounted.current) timer.current = window.setTimeout(() => void poll(), 1500);
  };

  const poll = async () => {
    window.clearTimeout(timer.current);
    try {
      const s = await api.streamCheck();
      if (!mounted.current) return;
      setSt(s);
      if (s.running) schedule();
    } catch {
      /* the next start or reopen retries */
    }
  };

  useEffect(() => {
    mounted.current = true;
    void poll();
    return () => {
      mounted.current = false;
      window.clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const start = async () => {
    setError(null);
    try {
      const s = await api.startStreamCheck();
      if (!mounted.current) return;
      setSt(s);
      schedule();
    } catch (e) {
      if (mounted.current) setError(e instanceof Error ? e.message : "Could not start");
    }
  };

  const running = Boolean(st?.running);
  const problems = (st?.results ?? []).filter((r) => !r.ok).sort((a, b) => a.name.localeCompare(b.name));
  const checked = st && !running && st.checked_at;

  const changed = (id: string, patch: Partial<StreamCheckResult> | null) => {
    setSt((s) =>
      s && {
        ...s,
        results: patch
          ? s.results.map((r) => (r.id === id ? { ...r, ...patch } : r))
          : s.results.filter((r) => r.id !== id),
      }
    );
    window.dispatchEvent(new Event("safydo:streams-changed"));
  };

  return (
    <div className="py-2">
      <div className={row}>
        <div className="min-w-0">
          <p className="font-medium">Check stations</p>
          <p className="text-xs text-mist">
            {running
              ? `Checking ${st!.done}/${st!.total} · ${problems.length} not answering so far`
              : checked
                ? problems.length
                  ? `${problems.length} of ${st!.total} not answering · checked ${new Date(st!.checked_at! * 1000).toLocaleTimeString()}`
                  : `All ${st!.total} answer · checked ${new Date(st!.checked_at! * 1000).toLocaleTimeString()}`
                : "Opens every station once to see which are offline"}
          </p>
          {error && <p className="text-xs text-red-300">{error}</p>}
        </div>
        <button
          type="button"
          disabled={running}
          onClick={() => void start()}
          className="flex shrink-0 items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Activity className="h-3.5 w-3.5" />}
          {checked ? "Check again" : "Check all"}
        </button>
      </div>
      {problems.length > 0 && (
        <ul className="mt-1 max-h-80 space-y-1.5 overflow-y-auto pr-1">
          {problems.map((r) => (
            <ProblemRow key={r.id} r={r} onChanged={changed} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ProblemRow({
  r,
  onChanged,
}: {
  r: StreamCheckResult;
  onChanged: (id: string, patch: Partial<StreamCheckResult> | null) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const [alts, setAlts] = useState<ProbeCandidate[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className={`rounded-lg border border-white/10 bg-black/20 p-2 ${r.disabled ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">
            {r.name}
            {r.disabled && <span className="ml-1.5 text-[10px] font-normal text-amber-300">hidden</span>}
          </p>
          <p className="truncate font-mono text-[10px] text-mist" title={r.url}>
            {r.url}
          </p>
          <p className="text-[11px] text-red-300">{r.reason}</p>
        </div>
        <DeleteButton
          armed={armed}
          what="station"
          disabled={busy !== null}
          onClick={() =>
            armed
              ? void run("delete", async () => {
                  await api.deleteStream(r.id);
                  onChanged(r.id, null);
                })
              : setArmed(true)
          }
        />
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button
          type="button"
          className={btn}
          disabled={busy !== null}
          title="Try it in the player"
          onClick={() => void run("play", () => api.playStream(r.id).then(() => setMsg("It played — the check may have caught a hiccup")))}
        >
          {busy === "play" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />} Try
        </button>
        <button
          type="button"
          className={btn}
          disabled={busy !== null}
          title="Look for a working address: the station's website, then Radio Browser"
          onClick={() =>
            void run("alts", async () => {
              const found = await api.streamAlternatives(r.id);
              setAlts(found);
              if (!found.length) setMsg("No working replacement found");
            })
          }
        >
          {busy === "alts" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Search className="h-3 w-3" />} Find replacement
        </button>
        <button
          type="button"
          className={btn}
          disabled={busy !== null}
          title={r.disabled ? "Show it in the station list again" : "Hide it from the station list, keep it saved"}
          onClick={() =>
            void run("hide", async () => {
              await api.patchStream(r.id, { disabled: !r.disabled });
              onChanged(r.id, { disabled: !r.disabled });
            })
          }
        >
          {r.disabled ? <Eye className="h-3 w-3" /> : <EyeOff className="h-3 w-3" />} {r.disabled ? "Show" : "Hide"}
        </button>
      </div>
      {msg && <p className="mt-1 text-[11px] text-mist">{msg}</p>}
      {alts && alts.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {alts.map((c) => (
            <li key={c.url} className="flex items-center gap-2 rounded-md bg-white/5 px-2 py-1">
              <span className="min-w-0 flex-1">
                <span className="block truncate font-mono text-[10px]" title={c.url}>
                  {c.url}
                </span>
                <span className="text-[10px] text-mist">
                  {[c.name, c.format, c.bitrate ? `${c.bitrate} kbps` : null].filter(Boolean).join(" · ")}
                </span>
              </span>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() =>
                  void run("use", async () => {
                    await api.patchStream(r.id, { url: c.url, ...(r.disabled ? { disabled: false } : {}) });
                    onChanged(r.id, null);
                  })
                }
                className="flex shrink-0 items-center gap-1 rounded-md bg-[var(--accent)] px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
              >
                <Check className="h-3 w-3" /> Use
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
