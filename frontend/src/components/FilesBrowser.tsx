import {
  ChevronRight,
  Folder,
  HardDrive,
  Loader2,
  Music2,
  Plus,
  Play,
  Plug,
  RefreshCw,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useCollapsed } from "../hooks/useCollapsed";
import { CollapseToggle } from "./CollapseToggle";
import { api } from "../api/client";
import type { AddDrivePayload, BrowseResult, FileSource } from "../types";

interface Props {
  onPlay: () => void;
}

const BLANK_DRIVE: AddDrivePayload = {
  type: "smb",
  host: "",
  share: "",
  label: "",
  username: "",
  password: "",
  domain: "",
};

function SourceIcon({ type }: { type: FileSource["type"] }) {
  if (type === "usb") return <HardDrive className="h-3.5 w-3.5" />;
  return <Server className="h-3.5 w-3.5" />;
}

export function FilesBrowser({ onPlay }: Props) {
  const [sources, setSources] = useState<FileSource[]>([]);
  const [collapsed, toggleCollapsed] = useCollapsed("files");
  const [active, setActive] = useState("usb");
  const [path, setPath] = useState("");
  const [view, setView] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyDrive, setBusyDrive] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [form, setForm] = useState<AddDrivePayload>(BLANK_DRIVE);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const activeSource = sources.find((s) => s.id === active);

  const loadSources = useCallback(async () => {
    try {
      const list = await api.filesSources();
      setSources(list);
      return list;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load sources");
      return [];
    }
  }, []);

  // Only the newest listing lands: a slow folder must not replace the one
  // clicked after it (the breadcrumb would name one folder, the list another).
  const browseSeq = useRef(0);
  const browse = useCallback(async (source: string, at: string) => {
    const seq = ++browseSeq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await api.filesBrowse(source, at);
      if (seq === browseSeq.current) setView(res);
    } catch (e) {
      if (seq !== browseSeq.current) return;
      setView(null);
      setError(e instanceof Error ? e.message : "Cannot open folder");
    } finally {
      if (seq === browseSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const mounted = Boolean(activeSource?.mounted);
  useEffect(() => {
    if (mounted) void browse(active, path);
    else {
      browseSeq.current++; // a listing still in flight is for a source that went away
      setView(null);
    }
  }, [active, path, mounted, browse]);

  // Sources are re-read too: a USB stick plugged in after the page loaded
  // only shows up that way, and that is exactly when nothing is mounted.
  const refresh = async () => {
    const list = await loadSources();
    // Newly mounted: the effect lists it. Still mounted: nothing re-runs it.
    if (mounted && list.find((s) => s.id === active)?.mounted) await browse(active, path);
  };

  const openSource = (id: string) => {
    setActive(id);
    setPath("");
    setShowAdd(false);
  };

  const play = async (filePath: string) => {
    try {
      await api.playFile(filePath);
      onPlay();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Playback failed");
    }
  };

  const mount = async (id: string) => {
    setBusyDrive(id);
    setError(null);
    try {
      await api.filesMountDrive(id);
      // The listing follows from the effect once the source reads as mounted.
      setPath("");
      await loadSources();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Mount failed");
    } finally {
      setBusyDrive(null);
    }
  };

  const remove = async (id: string, label: string) => {
    if (!confirm(`Remove network drive "${label}"?`)) return;
    setBusyDrive(id);
    try {
      await api.filesRemoveDrive(id);
      const list = await loadSources();
      if (active === id) openSource(list[0]?.id ?? "usb");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      setBusyDrive(null);
    }
  };

  const submitDrive = async (e: React.FormEvent) => {
    e.preventDefault();
    setAdding(true);
    setAddError(null);
    try {
      const res = await api.filesAddDrive({
        ...form,
        host: form.host.trim(),
        share: form.share.trim(),
      });
      await loadSources();
      setShowAdd(false);
      setForm(BLANK_DRIVE);
      openSource(res.id);
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Could not add drive");
    } finally {
      setAdding(false);
    }
  };

  const crumbs = path ? path.split("/") : [];

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Folder className="h-4 w-4 text-mist" />
          <h2 className="font-display text-sm font-semibold">Files</h2>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="rounded-lg p-2 text-mist hover:bg-white/5 hover:text-white disabled:opacity-40"
            aria-label="Refresh"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Files" />
        </div>
      </div>

      {/* Source chips */}
      <div className="mb-3 flex flex-wrap gap-2">
        {sources.map((s) => (
          <div key={s.id} className="group relative">
            <button
              type="button"
              onClick={() => openSource(s.id)}
              className={`flex items-center gap-1.5 rounded-lg py-1.5 pl-2.5 pr-2.5 text-xs transition-colors ${
                active === s.id
                  ? "bg-[var(--accent)] text-white"
                  : "bg-white/5 text-mist hover:bg-white/10"
              } ${s.type !== "usb" ? "pr-6" : ""}`}
            >
              <SourceIcon type={s.type} />
              <span className="max-w-[9rem] truncate">{s.label}</span>
              {!s.mounted && (
                <span className="ml-0.5 h-1.5 w-1.5 rounded-full bg-amber-400" title="Not mounted" />
              )}
            </button>
            {s.type !== "usb" && (
              <button
                type="button"
                onClick={() => void remove(s.id, s.label)}
                disabled={busyDrive === s.id}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-white/70 opacity-0 transition-opacity hover:text-red-300 group-hover:opacity-100"
                aria-label={`Remove ${s.label}`}
              >
                <Trash2 className="h-3 w-3" />
              </button>
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={() => {
            setShowAdd((v) => !v);
            setAddError(null);
          }}
          className="flex items-center gap-1 rounded-lg bg-white/5 px-2.5 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white"
        >
          <Plus className="h-3.5 w-3.5" />
          Network drive
        </button>
      </div>

      {/* Add-drive form */}
      {showAdd && (
        <form
          onSubmit={submitDrive}
          className="mb-3 space-y-2 rounded-xl border border-white/10 bg-black/20 p-3"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold">Add network drive</span>
            <button type="button" onClick={() => setShowAdd(false)} className="text-mist hover:text-white">
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="flex gap-2">
            {(["smb", "nfs"] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setForm((f) => ({ ...f, type: t }))}
                className={`flex-1 rounded-lg py-1.5 text-xs uppercase transition-colors ${
                  form.type === t ? "bg-[var(--accent)] text-white" : "bg-white/5 text-mist hover:bg-white/10"
                }`}
              >
                {t === "smb" ? "SMB / Windows" : "NFS"}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <input
              required
              value={form.host}
              onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))}
              placeholder="Server (nas.local or IP)"
              className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs outline-none focus:border-[var(--accent)]"
            />
            <input
              required
              value={form.share}
              onChange={(e) => setForm((f) => ({ ...f, share: e.target.value }))}
              placeholder={form.type === "smb" ? "Share (music)" : "Export (/volume1/music)"}
              className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs outline-none focus:border-[var(--accent)]"
            />
            <input
              value={form.label}
              onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
              placeholder="Name (optional)"
              className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs outline-none focus:border-[var(--accent)] sm:col-span-2"
            />
            {form.type === "smb" && (
              <>
                <input
                  value={form.username}
                  onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
                  placeholder="Username (blank = guest)"
                  autoComplete="off"
                  className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs outline-none focus:border-[var(--accent)]"
                />
                <input
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                  placeholder="Password"
                  autoComplete="new-password"
                  className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs outline-none focus:border-[var(--accent)]"
                />
              </>
            )}
          </div>
          {addError && <p className="text-xs text-red-300">{addError}</p>}
          <button
            type="submit"
            disabled={adding}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent)] py-2 text-xs font-medium text-white disabled:opacity-50"
          >
            {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            {adding ? "Mounting…" : "Add & mount"}
          </button>
          <p className="text-[10px] text-mist">
            Mounted read-only. SMB password is stored only on the Pi, never shown again.
          </p>
        </form>
      )}

      {/* Breadcrumb */}
      {activeSource?.mounted && (
        <div className="mb-2 flex flex-wrap items-center gap-1 text-xs text-mist">
          <button
            type="button"
            onClick={() => setPath("")}
            className={`rounded px-1.5 py-0.5 hover:text-white ${!path ? "text-white" : ""}`}
          >
            {activeSource.label}
          </button>
          {crumbs.map((c, i) => {
            const to = crumbs.slice(0, i + 1).join("/");
            return (
              <span key={to} className="flex items-center gap-1">
                <ChevronRight className="h-3 w-3 opacity-50" />
                <button
                  type="button"
                  onClick={() => setPath(to)}
                  className={`rounded px-1.5 py-0.5 hover:text-white ${i === crumbs.length - 1 ? "text-white" : ""}`}
                >
                  {c}
                </button>
              </span>
            );
          })}
        </div>
      )}

      {error && <p className="mb-2 text-xs text-red-300">{error}</p>}

      {/* Body */}
      {!activeSource ? (
        <p className="py-6 text-center text-xs text-mist">No sources</p>
      ) : !activeSource.mounted ? (
        <div className="flex flex-col items-center gap-3 py-6">
          <p className="text-center text-xs text-mist">
            {activeSource.type === "usb" ? "No USB drive mounted" : `${activeSource.label} is not mounted`}
          </p>
          {activeSource.type !== "usb" && (
            <button
              type="button"
              onClick={() => void mount(activeSource.id)}
              disabled={busyDrive === activeSource.id}
              className="flex items-center gap-2 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
            >
              {busyDrive === activeSource.id ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plug className="h-3.5 w-3.5" />
              )}
              Mount
            </button>
          )}
        </div>
      ) : loading && !view ? (
        <p className="py-6 text-center text-xs text-mist">Loading…</p>
      ) : (
        <ul className="max-h-72 space-y-1 overflow-y-auto">
          {view?.dirs.map((d) => (
            <li key={d.rel}>
              <button
                type="button"
                onClick={() => setPath(d.rel)}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-white/5"
              >
                <Folder className="h-4 w-4 shrink-0 text-[var(--accent)]" />
                <span className="truncate text-xs">{d.name}</span>
                <ChevronRight className="ml-auto h-3.5 w-3.5 shrink-0 text-mist" />
              </button>
            </li>
          ))}
          {view?.files.map((f) => (
            <li key={f.path}>
              <button
                type="button"
                onClick={() => void play(f.path)}
                className="group flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-white/5"
              >
                <Music2 className="h-4 w-4 shrink-0 text-mist" />
                <span className="truncate text-xs">{f.name}</span>
                <Play className="ml-auto h-3.5 w-3.5 shrink-0 text-mist opacity-0 group-hover:opacity-100" />
              </button>
            </li>
          ))}
          {view && view.dirs.length === 0 && view.files.length === 0 && (
            <li className="py-6 text-center text-xs text-mist">Empty folder</li>
          )}
        </ul>
      )}
    </section>
  );
}
