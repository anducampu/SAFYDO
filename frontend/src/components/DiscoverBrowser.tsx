import { Check, Loader2, Play, Plus, Search } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { genreAccent } from "../lib/genreColors";
import type { DiscoverStation } from "../types";

interface Props {
  sourceId: string;
  configured: boolean;
  needsConfig: boolean;
  onPlay: () => void;
  onCount: (n: number) => void;
}

export function DiscoverBrowser({ sourceId, configured, needsConfig, onPlay, onCount }: Props) {
  const [genres, setGenres] = useState<string[]>([]);
  const [genre, setGenre] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<DiscoverStation[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [busyUrl, setBusyUrl] = useState<string | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Each load bumps this; an answer that is no longer the newest is dropped,
  // so a slow "ab" can't land after "abc", nor an old page after a genre change.
  const loadSeq = useRef(0);

  useEffect(() => {
    setGenres([]);
    setGenre(null);
    setQuery("");
    setItems([]);
    setSaved(new Set());
    if (!configured) return;
    api.sourceGenres(sourceId).then(setGenres).catch(() => setGenres([]));
  }, [sourceId, configured]);

  const load = useCallback(
    async (pg: number, replace: boolean) => {
      if (!configured) return;
      const seq = ++loadSeq.current;
      setLoading(true);
      setError(null);
      try {
        const res = await api.sourceBrowse(sourceId, { genre: genre ?? undefined, q: query.trim() || undefined, page: pg });
        if (seq !== loadSeq.current) return;
        setMore(res.length >= 50);
        setItems((prev) => (replace ? res : [...prev, ...res]));
        setPage(pg);
      } catch (e) {
        if (seq !== loadSeq.current) return;
        setError(e instanceof Error ? e.message : "Could not load");
        if (replace) setItems([]);
      } finally {
        if (seq === loadSeq.current) setLoading(false);
      }
    },
    [sourceId, genre, query, configured]
  );

  // (re)load on genre change, and debounced on query change
  useEffect(() => {
    if (!configured) return;
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => void load(0, true), query ? 350 : 0);
    return () => clearTimeout(searchTimer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [genre, query, sourceId, configured]);

  useEffect(() => onCount(items.length), [items.length, onCount]);

  const play = async (s: DiscoverStation) => {
    setBusyUrl(s.url);
    setError(null);
    try {
      await api.playUrl(s.url, s.name, s.genre, s.live ?? undefined);
      onPlay();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Playback failed");
    } finally {
      setBusyUrl(null);
    }
  };

  const save = async (s: DiscoverStation) => {
    try {
      const added = await api.addStream({
        name: s.name,
        url: s.url,
        genre: s.genre,
        category: sourceId,
        favorite: false,
        homepage: s.homepage ?? undefined,
      });
      setSaved((prev) => new Set(prev).add(s.url));
      // The directory usually knows the logo already; fetched in the background.
      void api.findLogo(added.id, { hint: s.favicon ?? undefined }).catch(() => {});
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    }
  };

  if (needsConfig && !configured) {
    return (
      <p className="py-8 text-center text-sm text-mist">
        This source needs setup — add its API key in <span className="text-white">Settings → Sources</span>.
      </p>
    );
  }

  return (
    <>
      <div className="relative mb-3">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-mist" />
        <input
          type="search"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-xl border border-white/5 bg-surface-2 py-2.5 pl-10 pr-4 text-sm outline-none ring-[var(--accent)] focus:ring-1"
        />
      </div>

      {genres.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setGenre(null)}
            className={`rounded-full px-3 py-1 text-xs font-medium ${!genre ? "bg-white/15 text-white" : "bg-white/5 text-mist"}`}
          >
            Top
          </button>
          {genres.map((g) => {
            const c = genreAccent(g);
            const active = genre === g;
            return (
              <button
                key={g}
                type="button"
                onClick={() => setGenre(active ? null : g)}
                className="rounded-full px-3 py-1 text-xs font-medium capitalize transition-colors"
                style={{ background: active ? `${c}44` : `${c}22`, color: c, boxShadow: active ? `inset 0 0 0 1px ${c}` : undefined }}
              >
                {g}
              </button>
            );
          })}
        </div>
      )}

      {error && (
        <p className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</p>
      )}

      {loading && items.length === 0 ? (
        <p className="py-8 text-center text-sm text-mist">Loading…</p>
      ) : items.length === 0 ? (
        <p className="py-8 text-center text-sm text-mist">No results.</p>
      ) : (
        <ul className="max-h-[min(50vh,420px)] space-y-1 overflow-y-auto pr-1">
          {items.map((s) => {
            const c = genreAccent(s.genre);
            const isSaved = saved.has(s.url);
            return (
              <li key={`${s.id}-${s.url}`}>
                <div className="group flex items-center gap-3 rounded-xl px-3 py-2.5 transition-colors hover:bg-white/5">
                  <button
                    type="button"
                    onClick={() => void play(s)}
                    disabled={busyUrl === s.url}
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold uppercase disabled:opacity-50"
                    style={{ background: `${c}22`, color: c }}
                    aria-label={`Play ${s.name}`}
                  >
                    {busyUrl === s.url ? <Loader2 className="h-4 w-4 animate-spin" /> : (s.genre.slice(0, 2) || "▶")}
                  </button>
                  <button type="button" onClick={() => void play(s)} className="min-w-0 flex-1 text-left">
                    <span className="block truncate text-sm font-medium">{s.name}</span>
                    <span className="mt-0.5 flex items-center gap-1.5">
                      <span
                        className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
                        style={{ background: `${c}22`, color: c }}
                      >
                        {s.genre}
                      </span>
                      {s.bitrate ? <span className="text-[10px] text-mist">{s.bitrate}k</span> : null}
                      {s.country ? <span className="text-[10px] text-mist">{s.country}</span> : null}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => void save(s)}
                    disabled={isSaved}
                    className="shrink-0 rounded-lg p-1.5 text-mist transition-colors hover:bg-white/10 hover:text-white disabled:opacity-100"
                    title={isSaved ? "Saved to My stations" : "Save to My stations"}
                  >
                    {isSaved ? <Check className="h-4 w-4 text-[var(--accent)]" /> : <Plus className="h-4 w-4" />}
                  </button>
                  <Play className="h-4 w-4 shrink-0 text-mist opacity-0 transition-opacity group-hover:opacity-100" />
                </div>
              </li>
            );
          })}
          {more && (
            <li className="pt-1">
              <button
                type="button"
                onClick={() => void load(page + 1, false)}
                disabled={loading}
                className="w-full rounded-xl bg-white/5 py-2 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            </li>
          )}
        </ul>
      )}
    </>
  );
}
