import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  ListPlus,
  Loader2,
  Play,
  Plus,
  Repeat,
  Shuffle,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { DeleteButton } from "./DeleteButton";
import { useSessionState } from "../hooks/useSessionState";
import type { WebLink, WebLinkPlaylist, WebLinksResponse } from "../types";
import { PLATFORM_COLORS, Thumb, duration } from "./linkShared";

interface Props {
  data: WebLinksResponse;
  reload: () => void;
  onPlay: () => void;
  onError: (msg: string | null) => void;
  /** Playlist playing now and the index (in mpv's queue) of its current entry. */
  playingId?: string;
  playingPos?: number | null;
}

function total(links: WebLink[]): string | null {
  const secs = links.reduce((sum, l) => sum + (l.duration || 0), 0);
  return secs ? duration(secs) : null;
}

function ModeButton({
  on,
  onClick,
  label,
  children,
}: {
  on: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      aria-label={label}
      title={label}
      className={`rounded-lg p-2 transition-colors ${
        on ? "text-[var(--accent)]" : "text-mist hover:bg-white/5 hover:text-white"
      }`}
      style={on ? { background: "color-mix(in srgb, var(--accent) 20%, transparent)" } : undefined}
    >
      {children}
    </button>
  );
}

function Editor({
  playlist,
  data,
  save,
  playFrom,
  playingIndex,
  onError,
  reload,
}: {
  playlist: WebLinkPlaylist;
  data: WebLinksResponse;
  save: (patch: Partial<Pick<WebLinkPlaylist, "name" | "link_ids">>) => Promise<void>;
  playFrom: (index: number) => void;
  playingIndex: number | null;
  onError: (msg: string | null) => void;
  reload: () => void;
}) {
  const byId = new Map(data.links.map((l) => [l.id, l]));
  const entries = playlist.link_ids.map((id) => byId.get(id)).filter((l): l is WebLink => Boolean(l));
  const available = data.links.filter((l) => !playlist.link_ids.includes(l.id));
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [showPicker, setShowPicker] = useState(false);

  const move = (from: number, to: number) => {
    if (to < 0 || to >= playlist.link_ids.length) return;
    const ids = [...playlist.link_ids];
    const [id] = ids.splice(from, 1);
    ids.splice(to, 0, id);
    void save({ link_ids: ids });
  };

  const addNew = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = url.trim();
    if (!value) return;
    setAdding(true);
    onError(null);
    try {
      // Into the library first (it reads title/duration), then onto the end.
      const link = await api.addLink(value);
      await save({ link_ids: [...playlist.link_ids, link.id] });
      setUrl("");
      reload();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not add link");
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="mt-1 space-y-2 rounded-xl bg-black/20 p-2">
      {entries.length === 0 ? (
        <p className="py-3 text-center text-xs text-mist">Empty — add links below.</p>
      ) : (
        <ol className="space-y-0.5">
          {entries.map((link, i) => {
            const current = playingIndex === i;
            return (
              <li
                key={link.id}
                className={`flex items-center gap-1 rounded-lg pr-0.5 ${current ? "bg-white/[0.07]" : "hover:bg-white/5"}`}
              >
                <button
                  type="button"
                  onClick={() => playFrom(i)}
                  className="flex min-w-0 flex-1 items-center gap-2 px-1.5 py-1.5 text-left"
                  title="Play from here"
                >
                  <span className="w-5 shrink-0 text-right text-[11px] tabular-nums text-mist">{i + 1}</span>
                  <Thumb link={link} small />
                  <span className="min-w-0 flex-1">
                    <span
                      className="block truncate text-xs font-medium"
                      style={current ? { color: "var(--accent)" } : undefined}
                    >
                      {link.title}
                    </span>
                    <span className="block truncate text-[10px] text-mist">
                      <span style={{ color: PLATFORM_COLORS[link.platform] }}>
                        {data.platforms[link.platform] ?? link.platform}
                      </span>
                      {duration(link.duration) && ` · ${duration(link.duration)}`}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => move(i, i - 1)}
                  disabled={i === 0}
                  aria-label="Move up"
                  className="rounded p-1.5 text-mist hover:bg-white/5 hover:text-white disabled:opacity-25"
                >
                  <ArrowUp className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => move(i, i + 1)}
                  disabled={i === entries.length - 1}
                  aria-label="Move down"
                  className="rounded p-1.5 text-mist hover:bg-white/5 hover:text-white disabled:opacity-25"
                >
                  <ArrowDown className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => void save({ link_ids: playlist.link_ids.filter((id) => id !== link.id) })}
                  aria-label="Remove from playlist"
                  title="Remove from playlist (the link stays in your library)"
                  className="rounded p-1.5 text-mist hover:bg-white/5 hover:text-red-300"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            );
          })}
        </ol>
      )}

      <form onSubmit={(e) => void addNew(e)} className="flex gap-1.5">
        <input
          type="url"
          inputMode="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="Paste a link to add it here"
          disabled={adding}
          className="min-w-0 flex-1 rounded-lg border border-white/5 bg-surface-2 px-2.5 py-1.5 text-xs outline-none ring-[var(--accent)] focus:ring-1 disabled:opacity-60"
        />
        <button
          type="submit"
          disabled={adding || !url.trim()}
          className="flex shrink-0 items-center gap-1 rounded-lg bg-[var(--accent)] px-2.5 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {adding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Add
        </button>
      </form>

      {available.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowPicker((v) => !v)}
            className="flex items-center gap-1 text-[11px] text-mist hover:text-white"
          >
            {showPicker ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            From your links ({available.length})
          </button>
          {showPicker && (
            <ul className="mt-1 max-h-48 space-y-0.5 overflow-y-auto pr-1">
              {available.map((link) => (
                <li key={link.id}>
                  <button
                    type="button"
                    onClick={() => void save({ link_ids: [...playlist.link_ids, link.id] })}
                    className="flex w-full items-center gap-2 rounded-lg px-1.5 py-1 text-left hover:bg-white/5"
                  >
                    <ListPlus className="h-3.5 w-3.5 shrink-0 text-mist" />
                    <span className="min-w-0 flex-1 truncate text-xs">{link.title}</span>
                    <span className="shrink-0 text-[10px]" style={{ color: PLATFORM_COLORS[link.platform] }}>
                      {data.platforms[link.platform] ?? link.platform}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export function LinkPlaylists({ data, reload, onPlay, onError, playingId, playingPos }: Props) {
  const [name, setName] = useState("");
  const [openId, setOpenId] = useSessionState<string | null>("links.playlist.open", null);
  const [startingId, setStartingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  // Optimistic copy so reorders feel instant; reconciled on every reload.
  const [playlists, setPlaylists] = useState(data.playlists);
  useEffect(() => setPlaylists(data.playlists), [data.playlists]);

  useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(null), 4000);
    return () => clearTimeout(t);
  }, [confirmDelete]);

  const update = async (
    id: string,
    patch: Partial<Pick<WebLinkPlaylist, "name" | "link_ids" | "shuffle" | "loop">>
  ) => {
    setPlaylists((ps) => ps.map((p) => (p.id === id ? { ...p, ...patch } : p)));
    onError(null);
    try {
      await api.updateLinkPlaylist(id, patch);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not save playlist");
    }
    reload();
  };

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    onError(null);
    try {
      const p = await api.createLinkPlaylist(name.trim());
      setName("");
      setOpenId(p.id);
      reload();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not create playlist");
    }
  };

  const play = async (p: WebLinkPlaylist, start = 0) => {
    setStartingId(p.id);
    onError(null);
    try {
      await api.playLinkPlaylist(p.id, start);
      onPlay();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Playback failed");
    } finally {
      setStartingId(null);
    }
  };

  const remove = async (p: WebLinkPlaylist) => {
    if (confirmDelete !== p.id) {
      setConfirmDelete(p.id);
      return;
    }
    setConfirmDelete(null);
    try {
      await api.deleteLinkPlaylist(p.id);
      reload();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not delete playlist");
    }
  };

  const byId = new Map(data.links.map((l) => [l.id, l]));

  return (
    <div>
      <form onSubmit={(e) => void create(e)} className="mb-3 flex gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="New playlist name"
          maxLength={80}
          className="min-w-0 flex-1 rounded-xl border border-white/5 bg-surface-2 px-3 py-2.5 text-sm outline-none ring-[var(--accent)] focus:ring-1"
        />
        <button
          type="submit"
          disabled={!name.trim()}
          className="flex shrink-0 items-center gap-1.5 rounded-xl bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          <Plus className="h-4 w-4" /> Create
        </button>
      </form>

      {playlists.length === 0 ? (
        <p className="py-6 text-center text-sm text-mist">No playlists yet.</p>
      ) : (
        <ul className="max-h-[min(60vh,560px)] space-y-1 overflow-y-auto pr-1">
          {playlists.map((p) => {
            const entries = p.link_ids.map((id) => byId.get(id)).filter((l): l is WebLink => Boolean(l));
            const open = openId === p.id;
            const playing = playingId === p.id;
            const length = total(entries);
            const chevron = open ? (
              <ChevronDown className="h-4 w-4 shrink-0 text-mist" />
            ) : (
              <ChevronRight className="h-4 w-4 shrink-0 text-mist" />
            );
            const meta = (
              <span className="block text-[11px] text-mist">
                {entries.length} {entries.length === 1 ? "link" : "links"}
                {length && ` · ${length}`}
                {playing && playingPos != null && ` · playing ${playingPos + 1}/${entries.length}`}
              </span>
            );
            return (
              <li key={p.id} className={`rounded-xl ${playing ? "bg-white/[0.07]" : ""}`}>
                <div className="flex items-center gap-1 pr-1">
                  {/* As in RecordingsPanel: the field replaces the toggle button
                      instead of sitting inside it (invalid, and Firefox will not
                      place the caret there), and Escape goes through a flagged
                      blur, since unmounting a focused input can still fire onBlur
                      and save the draft that Escape meant to throw away. */}
                  {renaming?.id === p.id ? (
                    <div className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2">
                      {chevron}
                      <span className="min-w-0 flex-1">
                        <input
                          autoFocus
                          value={renaming.name}
                          maxLength={80}
                          aria-label="Playlist name"
                          onChange={(e) => setRenaming({ id: p.id, name: e.target.value })}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") e.currentTarget.blur();
                            if (e.key === "Escape") {
                              e.currentTarget.dataset.cancel = "1";
                              e.currentTarget.blur();
                            }
                          }}
                          onBlur={(e) => {
                            const cancelled = e.currentTarget.dataset.cancel === "1";
                            const next = renaming.name.trim();
                            setRenaming(null);
                            if (!cancelled && next && next !== p.name) void update(p.id, { name: next });
                          }}
                          className="w-full rounded bg-surface-2 px-1.5 py-0.5 text-sm font-medium outline-none ring-1 ring-[var(--accent)]"
                        />
                        {meta}
                      </span>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setOpenId(open ? null : p.id)}
                      aria-expanded={open}
                      className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left"
                    >
                      {chevron}
                      <span className="min-w-0 flex-1">
                        <span
                          className="block truncate text-sm font-medium"
                          style={playing ? { color: "var(--accent)" } : undefined}
                          onDoubleClick={(e) => {
                            e.stopPropagation();
                            setRenaming({ id: p.id, name: p.name });
                          }}
                          title="Double-click to rename"
                        >
                          {p.name}
                        </span>
                        {meta}
                      </span>
                    </button>
                  )}
                  <ModeButton on={p.shuffle} onClick={() => void update(p.id, { shuffle: !p.shuffle })} label="Shuffle">
                    <Shuffle className="h-4 w-4" />
                  </ModeButton>
                  <ModeButton on={p.loop} onClick={() => void update(p.id, { loop: !p.loop })} label="Loop playlist">
                    <Repeat className="h-4 w-4" />
                  </ModeButton>
                  <button
                    type="button"
                    onClick={() => void play(p)}
                    disabled={startingId !== null || entries.length === 0}
                    aria-label={`Play ${p.name}`}
                    className="rounded-lg p-2 text-mist hover:bg-white/5 hover:text-white disabled:opacity-40"
                  >
                    {startingId === p.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  </button>
                  <DeleteButton armed={confirmDelete === p.id} onClick={() => void remove(p)} what="playlist" />
                </div>
                {open && (
                  <Editor
                    playlist={p}
                    data={data}
                    save={(patch) => update(p.id, patch)}
                    playFrom={(i) => void play(p, i)}
                    playingIndex={playing && !p.shuffle ? (playingPos ?? null) : null}
                    onError={onError}
                    reload={reload}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
