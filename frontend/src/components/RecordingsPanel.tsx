import { Disc3, Download, HardDrive, Loader2, Pencil, Play, Square, Usb } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { DeleteButton } from "./DeleteButton";
import { useCollapsed } from "../hooks/useCollapsed";
import { clock } from "../lib/clock";
import type { Recording, RecordingStatus, RecordingsResponse } from "../types";
import { CollapseToggle } from "./CollapseToggle";

interface Props {
  /** Live recorder state from the 1 Hz status, so the list refreshes when one finishes. */
  recording?: RecordingStatus;
  onPlay: () => void;
  /** Path of the file playing now, to highlight its row. */
  playingPath?: string;
}

export function bytes(n?: number | null): string {
  if (!n) return "0 MB";
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export function RecordingsPanel({ recording, onPlay, playingPath }: Props) {
  const [collapsed, toggleCollapsed] = useCollapsed("recordings");
  const [data, setData] = useState<RecordingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);

  const load = useCallback(() => {
    api
      .recordings()
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load recordings"));
  }, []);

  // On mount, and when a recording finishes (or starts): that changes the list.
  useEffect(load, [load, recording?.active]);

  useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(null), 4000);
    return () => clearTimeout(t);
  }, [confirmDelete]);

  const act = async (id: string, fn: () => Promise<unknown>, after?: () => void) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      after?.();
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusyId(null);
    }
  };

  const remove = (r: Recording) => {
    if (confirmDelete !== r.id) {
      setConfirmDelete(r.id);
      return;
    }
    setConfirmDelete(null);
    void act(r.id, () => api.deleteRecording(r.id));
  };

  const items = data?.items ?? [];
  // Mirrors Recorder.target_dir(): "usb" never falls back to the SD card.
  const location = data?.settings.location;
  const usbMissing = location === "usb" && !data?.usb_available;
  const where = location === "sd" || (location !== "usb" && !data?.usb_available) ? "sd" : "usb";
  const space = data?.space?.[where];

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold">
          <Disc3 className="h-4 w-4 text-mist" /> Recordings{" "}
          <span className="font-normal text-mist">({items.length})</span>
        </h2>
        <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Recordings" />
      </div>

      <div>
        <p className="mb-3 flex items-center gap-1.5 text-[11px] text-mist">
          {where === "usb" ? <Usb className="h-3 w-3" /> : <HardDrive className="h-3 w-3" />}
          {usbMissing
            ? "Set to record to a USB drive, but none is mounted — plug one in or pick another place in Settings."
            : <>
                New recordings go to {where === "usb" ? "the USB drive" : "the SD card"}
                {space && ` · ${bytes(space.free)} free`} · {data?.settings.format.toUpperCase() ?? "…"}
                {" — press ● in Playback to record what is playing."}
              </>}
        </p>

        {recording?.active && (
          <div className="mb-3 flex items-center gap-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2">
            <span className="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-red-400" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{recording.label}</span>
              <span className="block text-[11px] text-red-200/80">
                {clock(recording.elapsed)} · {bytes(recording.bytes)} · {recording.format?.toUpperCase()}{" "}
                {recording.quality}
                {recording.max_minutes ? ` · stops at ${recording.max_minutes} min` : ""}
              </span>
            </span>
            <button
              type="button"
              onClick={() => void act("stop", () => api.stopRecording())}
              className="flex items-center gap-1 rounded-lg bg-red-500/20 px-2.5 py-1.5 text-xs font-medium text-red-200 hover:bg-red-500/30"
            >
              {busyId === "stop" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
              Stop
            </button>
          </div>
        )}
        {!recording?.active && recording?.error && (
          <p className="mb-3 rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-100">
            {recording.error}
          </p>
        )}
        {error && (
          <p className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            {error}
          </p>
        )}

        {data && items.length === 0 ? (
          <p className="py-6 text-center text-sm text-mist">No recordings yet.</p>
        ) : (
          <ul className="max-h-[min(50vh,420px)] space-y-1 overflow-y-auto pr-1">
            {items.map((r) => {
              const current = Boolean(playingPath && playingPath.endsWith("/" + r.file));
              return (
                <li
                  key={r.id}
                  className={`flex items-center gap-1 rounded-xl pr-1 ${current ? "bg-white/[0.07]" : "hover:bg-white/5"}`}
                >
                  {/* The rename field replaces the play button rather than
                      sitting inside it: a double-click there fired two plays
                      first, and an input inside a <button> is invalid. */}
                  {renaming?.id === r.id ? (
                    <div className="flex min-w-0 flex-1 items-center gap-3 px-2 py-2">
                      <Pencil className="h-4 w-4 shrink-0 text-mist" />
                      <input
                        autoFocus
                        value={renaming.name}
                        maxLength={80}
                        aria-label="New name"
                        onChange={(e) => setRenaming({ id: r.id, name: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") e.currentTarget.blur();
                          if (e.key === "Escape") {
                            // Through blur, flagged: unmounting a focused input
                            // can still fire onBlur, which would save the draft.
                            e.currentTarget.dataset.cancel = "1";
                            e.currentTarget.blur();
                          }
                        }}
                        onBlur={(e) => {
                          const cancelled = e.currentTarget.dataset.cancel === "1";
                          const next = renaming.name.trim();
                          setRenaming(null);
                          if (!cancelled && next && next !== r.name) {
                            void act(r.id, () => api.renameRecording(r.id, next));
                          }
                        }}
                        className="min-w-0 flex-1 rounded bg-surface-2 px-1.5 py-0.5 text-sm outline-none ring-1 ring-[var(--accent)]"
                      />
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void act(r.id, () => api.playRecording(r.id), onPlay)}
                      disabled={busyId !== null}
                      className="flex min-w-0 flex-1 items-center gap-3 px-2 py-2 text-left disabled:opacity-60"
                    >
                      {busyId === r.id ? (
                        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-mist" />
                      ) : (
                        <Play className="h-4 w-4 shrink-0 text-mist" />
                      )}
                      <span className="min-w-0 flex-1">
                        <span
                          className="block truncate text-sm font-medium"
                          style={current ? { color: "var(--accent)" } : undefined}
                        >
                          {r.name}
                        </span>
                        <span className="block truncate text-[11px] text-mist">
                          {r.format.toUpperCase()}
                          {r.quality ? ` ${r.quality}` : ""} · {clock(r.duration)} · {bytes(r.bytes)} ·{" "}
                          {r.location === "usb" ? "USB" : "SD"}
                        </span>
                      </span>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setRenaming({ id: r.id, name: r.name })}
                    disabled={busyId !== null}
                    aria-label="Rename"
                    title="Rename"
                    className="rounded-lg p-2 text-mist hover:bg-white/5 hover:text-white disabled:opacity-40"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <a
                    href={api.recordingDownloadUrl(r.id)}
                    download={r.file}
                    aria-label="Download"
                    title="Download"
                    className="rounded-lg p-2 text-mist hover:bg-white/5 hover:text-white"
                  >
                    <Download className="h-4 w-4" />
                  </a>
                  <DeleteButton armed={confirmDelete === r.id} onClick={() => remove(r)} what="recording" />
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
