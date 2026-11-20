import { KeyRound, Link2, ListMusic, Loader2, Play, Plus, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { DeleteButton } from "./DeleteButton";
import { useCollapsed } from "../hooks/useCollapsed";
import { useSessionState } from "../hooks/useSessionState";
import type { WebLink, WebLinksResponse } from "../types";
import { CollapseToggle } from "./CollapseToggle";
import { LinkPlaylists } from "./LinkPlaylists";
import { PLATFORM_COLORS, Thumb, duration } from "./linkShared";

interface Props {
  onPlay: () => void;
  /** link_id of what is playing now, to highlight its row. */
  currentId?: string;
  /** Saved playlist playing now, and its current queue index. */
  playlistId?: string;
  playlistPos?: number | null;
}

/** Per-site account cookies (Netscape cookies.txt). Vimeo now refuses
 * anonymous playback; a logged-in session's cookies get past that. */
function Accounts({
  data,
  onChange,
  onError,
}: {
  data: WebLinksResponse;
  onChange: () => void;
  onError: (msg: string | null) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);

  const upload = async (platform: string, file: File | undefined) => {
    if (!file) return;
    setBusy(platform);
    onError(null);
    try {
      await api.setLinkCookies(platform, await file.text());
      onChange();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Could not save cookies");
    } finally {
      setBusy(null);
    }
  };

  const remove = async (platform: string) => {
    setBusy(platform);
    try {
      await api.deleteLinkCookies(platform);
      onChange();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Could not remove cookies");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mb-3 rounded-xl bg-black/20 p-3">
      <p className="mb-2 text-[11px] text-mist">
        For sites that need you logged in (Vimeo). Export cookies.txt (Netscape format) from a browser
        where you are signed in, e.g. with a "cookies.txt" extension, and upload it here. It stays on the
        Pi, is sent only to that site, and works until that session expires or you log out.
      </p>
      <ul className="space-y-1">
        {Object.entries(data.platforms).map(([id, label]) => {
          const saved = data.cookies?.[id];
          return (
            <li key={id} className="flex items-center justify-between gap-2 text-xs">
              <span>
                <span className="font-medium" style={{ color: PLATFORM_COLORS[id] }}>
                  {label}
                </span>
                <span className="text-mist">
                  {saved ? ` · saved ${new Date(saved.saved_at * 1000).toLocaleDateString()}` : " · none"}
                </span>
              </span>
              <span className="flex items-center gap-1">
                {busy === id && <Loader2 className="h-3.5 w-3.5 animate-spin text-mist" />}
                <label className="flex cursor-pointer items-center gap-1 rounded-lg px-2 py-1 text-mist hover:bg-white/5 hover:text-white">
                  <Upload className="h-3.5 w-3.5" /> {saved ? "Replace" : "Upload"}
                  <input
                    type="file"
                    accept=".txt,text/plain"
                    className="hidden"
                    disabled={busy !== null}
                    onChange={(e) => {
                      void upload(id, e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                </label>
                {saved && (
                  <button
                    type="button"
                    onClick={() => void remove(id)}
                    disabled={busy !== null}
                    aria-label={`Remove ${label} cookies`}
                    className="rounded-lg p-1 text-mist hover:bg-white/5 hover:text-red-300"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function LinksPanel({ onPlay, currentId, playlistId, playlistPos }: Props) {
  const [collapsed, toggleCollapsed] = useCollapsed("links");
  const [data, setData] = useState<WebLinksResponse | null>(null);
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAccounts, setShowAccounts] = useState(false);
  const [view, setView] = useSessionState<"links" | "playlists">("links.view", "links");

  const load = useCallback(() => {
    api
      .links()
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load links"));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    if (!confirmDelete) return;
    const t = setTimeout(() => setConfirmDelete(null), 4000);
    return () => clearTimeout(t);
  }, [confirmDelete]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const value = url.trim();
    if (!value) return;
    setAdding(true);
    setError(null);
    try {
      await api.addLink(value);
      setUrl("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add link");
    } finally {
      setAdding(false);
    }
  };

  const play = async (link: WebLink) => {
    setPlayingId(link.id);
    setError(null);
    try {
      await api.playLink(link.id);
      onPlay();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Playback failed");
    } finally {
      setPlayingId(null);
    }
  };

  const remove = async (link: WebLink) => {
    if (confirmDelete !== link.id) {
      setConfirmDelete(link.id);
      return;
    }
    setConfirmDelete(null);
    try {
      await api.deleteLink(link.id);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove link");
    }
  };

  const links = data?.links ?? [];
  const platforms = data ? Object.values(data.platforms).join(", ") : "";

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-display text-lg font-semibold">
          <Link2 className="h-4 w-4 text-[var(--accent)]" /> Web links{" "}
          <span className="font-normal text-mist">({links.length})</span>
        </h2>
        <div className="flex items-center gap-1">
          {!collapsed && (
            <div className="flex gap-0.5 rounded-full bg-black/20 p-0.5">
              {(
                [
                  ["links", "Links"],
                  ["playlists", `Playlists${data?.playlists?.length ? ` (${data.playlists.length})` : ""}`],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setView(id)}
                  className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                    view === id ? "bg-[var(--accent)] text-white" : "text-mist hover:text-white"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Web links" />
        </div>
      </div>

      <div>
        {data && !data.ytdl && (
          <p className="mb-3 rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 py-2 text-xs text-amber-100">
            yt-dlp is not installed on this board — re-run install.sh with internet access.
          </p>
        )}

        {view === "playlists" ? (
          <>
            {error && (
              <p className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
                {error}
              </p>
            )}
            {data && (
              <LinkPlaylists
                data={data}
                reload={load}
                onPlay={onPlay}
                onError={setError}
                playingId={playlistId}
                playingPos={playlistPos}
              />
            )}
          </>
        ) : (
          <>
        <form onSubmit={(e) => void add(e)} className="mb-2 flex gap-2">
          <input
            type="url"
            inputMode="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste a YouTube, SoundCloud, Mixcloud… link"
            disabled={adding}
            className="min-w-0 flex-1 rounded-xl border border-white/5 bg-surface-2 px-3 py-2.5 text-sm outline-none ring-[var(--accent)] focus:ring-1 disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={adding || !url.trim()}
            className="flex shrink-0 items-center gap-1.5 rounded-xl bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            Add
          </button>
        </form>
        <div className="mb-3 flex items-start justify-between gap-2">
          <p className="text-[11px] text-mist">
            {adding
              ? "Reading the link on the Pi — this can take 10 s or more…"
              : `${platforms || "YouTube, SoundCloud, Mixcloud, Vimeo, Dailymotion, Rumble"} · audio only`}
          </p>
          <button
            type="button"
            onClick={() => setShowAccounts((v) => !v)}
            aria-expanded={showAccounts}
            className={`flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors ${
              showAccounts ? "bg-white/15 text-white" : "bg-white/5 text-mist hover:text-white"
            }`}
          >
            <KeyRound className="h-3 w-3" /> Accounts
            {data && Object.keys(data.cookies ?? {}).length > 0 && ` (${Object.keys(data.cookies).length})`}
          </button>
        </div>

        {showAccounts && data && <Accounts data={data} onChange={load} onError={setError} />}

        {error && (
          <p className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
            {error}
          </p>
        )}

        {data && links.length === 0 ? (
          <p className="py-6 text-center text-sm text-mist">No links yet.</p>
        ) : (
          <ul className="max-h-[min(50vh,420px)] space-y-1 overflow-y-auto pr-1">
            {links.map((link) => {
              const color = PLATFORM_COLORS[link.platform] ?? "#8b8798";
              const current = link.id === currentId;
              const len = duration(link.duration);
              return (
                <li
                  key={link.id}
                  className={`flex items-center gap-2 rounded-xl pr-1 transition-colors ${
                    current ? "bg-white/[0.07]" : "hover:bg-white/5"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => void play(link)}
                    disabled={playingId !== null}
                    className="flex min-w-0 flex-1 items-center gap-3 px-2 py-2 text-left disabled:opacity-60"
                    title={link.url}
                  >
                    <Thumb link={link} />
                    <span className="min-w-0 flex-1">
                      <span
                        className="block truncate text-sm font-medium"
                        style={current ? { color: "var(--accent)" } : undefined}
                      >
                        {link.title}
                      </span>
                      <span className="mt-0.5 flex items-center gap-1.5 text-[11px] text-mist">
                        <span className="font-medium uppercase tracking-wide" style={{ color }}>
                          {data?.platforms[link.platform] ?? link.platform}
                        </span>
                        {link.uploader && <span className="truncate">· {link.uploader}</span>}
                        {link.playlist_count ? (
                          <span className="flex shrink-0 items-center gap-0.5">
                            · <ListMusic className="h-3 w-3" /> {link.playlist_count}
                          </span>
                        ) : (
                          len && <span className="shrink-0">· {len}</span>
                        )}
                      </span>
                    </span>
                    {playingId === link.id ? (
                      <Loader2 className="h-4 w-4 shrink-0 animate-spin text-mist" />
                    ) : (
                      <Play className="h-4 w-4 shrink-0 text-mist" />
                    )}
                  </button>
                  <DeleteButton armed={confirmDelete === link.id} onClick={() => void remove(link)} what="link" />
                </li>
              );
            })}
          </ul>
        )}
          </>
        )}
      </div>
    </section>
  );
}
