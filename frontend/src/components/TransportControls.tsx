import { Loader2, Pause, Play, SkipBack, SkipForward, Square } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { clock } from "../lib/clock";
import type { MpvStatus, RecordingStatus } from "../types";

interface Props {
  mpv: MpvStatus | undefined;
  /** Live radio: pausing would only make the next audio stale, so there is no pause. */
  live?: boolean;
  onAction: () => void;
  recording?: RecordingStatus;
  /** Recordings module on (Settings -> General -> Modules). */
  canRecord?: boolean;
  /** What plays is live (station, live link, FM): the only kind that can be
   * recorded — an on-demand item is already buffered ahead of the play head. */
  recordable?: boolean;
}

export function TransportControls({ mpv, live, onAction, recording, canRecord = true, recordable = true }: Props) {
  const [recBusy, setRecBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const idle = !mpv?.running || mpv.idle;
  const paused = Boolean(mpv?.paused);
  const state = idle ? "Stopped" : paused ? "Paused" : "Playing";
  // A queue: a saved link playlist, or a YouTube/SoundCloud playlist URL.
  const count = mpv?.playlist_count ?? 0;
  const pos = mpv?.playlist_pos ?? -1;
  const queue = !idle && !live && count > 1;

  const rec = Boolean(recording?.active);
  const toggleRecord = async () => {
    setRecBusy(true);
    setError(null);
    try {
      await (rec ? api.stopRecording() : api.startRecording());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Recording failed");
    } finally {
      setRecBusy(false);
      onAction();
    }
  };

  const step = async (forward: boolean) => {
    try {
      await (forward ? api.next() : api.prev());
    } catch {
      /* start/end of a non-looping queue */
    }
    onAction();
  };

  // Failures are shown, not left as unhandled rejections, and the status is
  // re-read either way so the buttons match what the player really did.
  const transport = async (fn: () => Promise<unknown>, what: string) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : `${what} failed`);
    }
    onAction();
  };

  const handleToggle = () => (idle ? undefined : transport(api.toggle, paused ? "Resume" : "Pause"));
  const handleStop = () => transport(api.stop, "Stop");

  return (
    <div className="glass rounded-2xl px-4 py-3">
      {/* Wraps: with prev/next and record there can be five buttons, more than
          a phone (or the narrow desktop column) fits beside the label. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-[7rem] flex-1">
          <p className="font-display text-xs uppercase tracking-[0.2em] text-mist">
            Playback
          </p>
          <p className="mt-0.5 truncate text-sm font-medium">
            {state}
            {queue && pos >= 0 && <span className="font-normal text-mist"> · {pos + 1}/{count}</span>}
            {rec && <span className="font-normal text-red-300"> · REC {clock(recording?.elapsed ?? 0)}</span>}
          </p>
          {error && <p className="truncate text-[11px] text-red-300">{error}</p>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {queue && (
            <button
              type="button"
              onClick={() => void step(false)}
              className="flex h-10 w-10 items-center justify-center rounded-full bg-white/5 text-mist transition-colors hover:text-white"
              aria-label="Previous"
            >
              <SkipBack className="h-4 w-4" />
            </button>
          )}
          {!live && <button
            type="button"
            onClick={() => void handleToggle()}
            disabled={idle}
            className="flex h-12 w-12 items-center justify-center rounded-full transition-transform active:scale-95 disabled:opacity-40"
            style={{ background: "var(--accent)", boxShadow: "0 6px 24px var(--accent-glow)" }}
            aria-label={paused ? "Resume" : "Pause"}
          >
            {paused ? (
              <Play className="h-5 w-5 fill-white text-white" />
            ) : (
              <Pause className="h-5 w-5 fill-white text-white" />
            )}
          </button>}
          {queue && (
            <button
              type="button"
              onClick={() => void step(true)}
              className="flex h-10 w-10 items-center justify-center rounded-full bg-white/5 text-mist transition-colors hover:text-white"
              aria-label="Next"
            >
              <SkipForward className="h-4 w-4" />
            </button>
          )}
          {/* A running recording keeps its stop button even with the module
              switched off: the Recordings panel is gone then too. */}
          {(rec || (canRecord && !idle)) && (
            <button
              type="button"
              onClick={() => void toggleRecord()}
              disabled={recBusy || (!rec && !recordable)}
              className={`flex h-10 w-10 items-center justify-center rounded-full transition-colors disabled:opacity-50 ${
                rec ? "bg-red-500/25 text-red-300" : "bg-white/5 text-mist hover:text-red-300"
              }`}
              aria-label={rec ? "Stop recording" : "Record"}
              aria-pressed={rec}
              title={
                rec
                  ? "Stop recording"
                  : recordable
                    ? "Record what is playing"
                    : "Recording is for live sources — stations, live links and FM"
              }
            >
              {recBusy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : rec ? (
                <Square className="h-3.5 w-3.5 fill-current" />
              ) : (
                <span className="h-3.5 w-3.5 rounded-full bg-current" />
              )}
            </button>
          )}
          <button
            type="button"
            onClick={() => void handleStop()}
            disabled={idle}
            className="flex h-10 w-10 items-center justify-center rounded-full bg-white/5 text-mist transition-colors hover:text-white disabled:opacity-40"
            aria-label="Stop"
          >
            <Square className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
