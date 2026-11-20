import { Heart, Loader2, Pencil, Play, Plus, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useSessionState } from "../hooks/useSessionState";
import { genreAccent } from "../lib/genreColors";
import { splitGenres } from "../lib/genres";
import type { Stream } from "../types";
import { AddStationModal } from "./AddStationModal";
import { StationLogo } from "./StationLogo";
import { StreamEditor } from "./StreamEditor";

interface Props {
  onPlay: () => void;
  onCount: (n: number) => void;
}

export function LocalStations({ onPlay, onCount }: Props) {
  const [streams, setStreams] = useState<Stream[]>([]);
  const [favoritesOnly, setFavoritesOnly] = useSessionState("stations.favorites", false);
  const [genre, setGenre] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [playError, setPlayError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [showAdd, setShowAdd] = useState(false);
  const [showEdit, setShowEdit] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .streams(favoritesOnly)
      .then((data) => {
        if (cancelled) return;
        setStreams(data);
        setLoadError(null);
      })
      // Said out loud: an unanswered list used to read as "No stations match."
      .catch((e) => !cancelled && setLoadError(e instanceof Error ? e.message : "Could not load stations"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [favoritesOnly, reloadKey]);

  const reload = () => setReloadKey((k) => k + 1);

  // Settings → Sources' logo pass announces its end; pick up the new logos.
  useEffect(() => {
    const onChanged = () => setReloadKey((k) => k + 1);
    window.addEventListener("safydo:streams-changed", onChanged);
    return () => window.removeEventListener("safydo:streams-changed", onChanged);
  }, []);

  // Toggled in place: the row stays put even in the Favorites view, so a
  // mis-tap can be undone with a second tap; the view settles on next load.
  const toggleFavorite = async (stream: Stream) => {
    const next = !stream.favorite;
    setStreams((list) => list.map((s) => (s.id === stream.id ? { ...s, favorite: next } : s)));
    try {
      await api.setFavorite(stream.id, next);
    } catch (e) {
      setStreams((list) => list.map((s) => (s.id === stream.id ? { ...s, favorite: !next } : s)));
      setPlayError(e instanceof Error ? e.message : "Could not update favorites");
    }
  };

  // A new station gets its logo looked up in the background; the list
  // refreshes when the search ends, found or not.
  const onAdded = (stream: Stream, hint?: string | null) => {
    reload();
    void api
      .findLogo(stream.id, { hint: hint ?? undefined })
      .then((r) => r.found && reload())
      .catch(() => {});
  };

  const genres = useMemo(() => {
    const set = new Set(streams.flatMap((s) => splitGenres(s.genre)));
    return Array.from(set).sort();
  }, [streams]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return streams.filter((s) => {
      if (genre && !splitGenres(s.genre).includes(genre)) return false;
      if (q && !s.name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [streams, genre, query]);

  useEffect(() => onCount(filtered.length), [filtered.length, onCount]);

  const play = async (stream: Stream) => {
    setPlayingId(stream.id);
    setPlayError(null);
    try {
      await api.playStream(stream.id);
      onPlay();
    } catch (e) {
      setPlayError(e instanceof Error ? e.message : "Playback failed");
    } finally {
      setPlayingId(null);
    }
  };

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="flex items-center gap-1.5 rounded-full bg-white/5 px-3 py-1.5 text-xs font-medium text-mist transition-colors hover:text-white"
        >
          <Plus className="h-3.5 w-3.5" /> Add
        </button>
        <button
          type="button"
          onClick={() => setShowEdit(true)}
          className="flex items-center gap-1.5 rounded-full bg-white/5 px-3 py-1.5 text-xs font-medium text-mist transition-colors hover:text-white"
        >
          <Pencil className="h-3.5 w-3.5" /> Edit
        </button>
        <button
          type="button"
          onClick={() => setFavoritesOnly((v) => !v)}
          className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${
            favoritesOnly ? "text-white" : "bg-white/5 text-mist hover:text-white"
          }`}
          style={favoritesOnly ? { background: "color-mix(in srgb, var(--accent) 40%, transparent)" } : undefined}
        >
          <Heart className={`h-3.5 w-3.5 ${favoritesOnly ? "fill-current" : ""}`} /> Favorites
        </button>
      </div>

      <div className="relative mb-3">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-mist" />
        <input
          type="search"
          placeholder="Search stations…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-xl border border-white/5 bg-surface-2 py-2.5 pl-10 pr-4 text-sm outline-none ring-[var(--accent)] focus:ring-1"
        />
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => setGenre(null)}
          className={`rounded-full px-3 py-1 text-xs font-medium ${!genre ? "bg-white/15 text-white" : "bg-white/5 text-mist"}`}
        >
          All
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

      {(loadError || playError) && (
        <p className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {loadError ?? playError}
        </p>
      )}

      {loading ? (
        <p className="py-8 text-center text-sm text-mist">Loading stations…</p>
      ) : filtered.length === 0 ? (
        <p className="py-8 text-center text-sm text-mist">No stations match.</p>
      ) : (
        <ul className="max-h-[min(50vh,420px)] space-y-1 overflow-y-auto pr-1">
          {filtered.map((s) => (
            <li key={s.id} className="group flex items-center rounded-xl transition-colors hover:bg-white/5">
              <button
                type="button"
                onClick={() => void play(s)}
                disabled={playingId === s.id}
                className="flex min-w-0 flex-1 items-center gap-3 py-2.5 pl-3 text-left disabled:opacity-50"
              >
                <StationLogo logo={s.logo} genre={s.genre} color={s.color} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{s.name}</span>
                  <span className="mt-0.5 flex flex-wrap gap-1">
                    {splitGenres(s.genre).map((g) => (
                      <span
                        key={g}
                        className="inline-block rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
                        style={{ background: `${genreAccent(g)}22`, color: genreAccent(g) }}
                      >
                        {g}
                      </span>
                    ))}
                  </span>
                </span>
              </button>
              <button
                type="button"
                onClick={() => void toggleFavorite(s)}
                aria-pressed={!!s.favorite}
                aria-label={s.favorite ? `Remove ${s.name} from favorites` : `Add ${s.name} to favorites`}
                title={s.favorite ? "Remove from favorites" : "Add to favorites"}
                className="shrink-0 rounded-lg p-2.5 transition-colors hover:bg-white/5"
              >
                <Heart
                  className={`h-4 w-4 transition-colors ${
                    s.favorite ? "fill-[var(--accent)] text-[var(--accent)]" : "text-mist hover:text-white"
                  }`}
                />
              </button>
              <button
                type="button"
                onClick={() => void play(s)}
                disabled={playingId === s.id}
                aria-label={`Play ${s.name}`}
                title="Play"
                className="mr-1 shrink-0 rounded-lg p-2.5 text-mist transition-colors hover:bg-white/5 hover:text-[var(--accent)] disabled:opacity-50"
              >
                {playingId === s.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              </button>
            </li>
          ))}
        </ul>
      )}

      <AddStationModal open={showAdd} onClose={() => setShowAdd(false)} onSaved={onAdded} />
      <StreamEditor open={showEdit} favoritesOnly={favoritesOnly} onClose={() => setShowEdit(false)} onSaved={reload} />
    </>
  );
}
