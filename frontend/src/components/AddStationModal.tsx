import { Check, Loader2, Plus, Search } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import type { ProbeCandidate, Stream } from "../types";
import { Modal } from "./Modal";

interface Props {
  open: boolean;
  onClose: () => void;
  /** [logoHint]: a logo URL the directory already knows for this station. */
  onSaved: (stream: Stream, logoHint?: string | null) => void;
}

const inputCls =
  "w-full rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-[var(--accent)]";

/** "afc.fm" or "https://www.radiozu.ro/" — a site, not a mount: worth a lookup first. */
function looksLikeWebsite(raw: string): boolean {
  const t = raw.trim();
  if (!t) return false;
  try {
    const u = new URL(/^https?:\/\//i.test(t) ? t : `https://${t}`);
    return (u.pathname === "" || u.pathname === "/") && !u.search && !u.port;
  } catch {
    return false;
  }
}

function hostOf(raw: string): string {
  try {
    return new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`).hostname.replace(/^www\./, "");
  } catch {
    return raw;
  }
}

export function AddStationModal({ open, onClose, onSaved }: Props) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [genre, setGenre] = useState("");
  const [category, setCategory] = useState("Radio");
  const [favorite, setFavorite] = useState(false);
  const [homepage, setHomepage] = useState("");
  const [logoHint, setLogoHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [finding, setFinding] = useState(false);
  const [found, setFound] = useState<ProbeCandidate[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setUrl("");
    setGenre("");
    setCategory("Radio");
    setFavorite(false);
    setHomepage("");
    setLogoHint(null);
    setFound([]);
    setPicked(null);
    setInfo(null);
    setError(null);
  };

  const close = () => {
    reset();
    onClose();
  };

  /** Fills the form from one discovered stream; typed fields are left alone. */
  const apply = (c: ProbeCandidate, site: string | null) => {
    setUrl(c.url);
    setPicked(c.url);
    if (!name.trim() && c.name) setName(c.name);
    if (!genre.trim() && c.genre) setGenre(c.genre);
    setHomepage(c.homepage || site || "");
    setLogoHint(c.logo_hint ?? null);
  };

  /** Looks the input up; returns the stream to save when the answer is unambiguous. */
  const find = async (): Promise<ProbeCandidate | null> => {
    const input = url.trim();
    if (!input) {
      setError("Type a stream URL or the station's website first");
      return null;
    }
    setFinding(true);
    setError(null);
    setInfo(null);
    setFound([]);
    try {
      const r = await api.probeStream(input);
      if (r.error || r.streams.length === 0) {
        setError(r.error ?? "No streams found");
        return null;
      }
      if (r.streams.length === 1) {
        apply(r.streams[0], r.homepage);
        setInfo(`Found the stream on ${hostOf(input)}`);
        return r.streams[0];
      }
      setFound(r.streams);
      setHomepage(r.homepage ?? "");
      if (!name.trim() && r.title) setName(r.title);
      setInfo(`${r.streams.length} streams on ${hostOf(input)}: pick one`);
      return null;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Lookup failed");
      return null;
    } finally {
      setFinding(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    let streamUrl = url.trim();
    let streamName = name.trim();
    let streamGenre = genre.trim();
    let site = homepage;
    let hint = logoHint;
    // A bare website: look for its stream first instead of saving a web page as one.
    if (!picked && looksLikeWebsite(streamUrl)) {
      const c = await find();
      if (!c) return; // nothing found, or several to choose from
      streamUrl = c.url;
      streamName = streamName || c.name || "";
      streamGenre = streamGenre || c.genre || "";
      site = c.homepage || site;
      hint = c.logo_hint ?? hint;
    }
    if (!streamName) streamName = hostOf(site || streamUrl);
    setBusy(true);
    setError(null);
    try {
      const stream = await api.addStream({
        name: streamName,
        url: streamUrl,
        genre: streamGenre,
        category: category.trim(),
        favorite,
        homepage: site || undefined,
      });
      reset();
      onSaved(stream, hint);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Add failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={close} title="Add station">
      <form onSubmit={submit} className="space-y-3">
        <label className="block space-y-1">
          <span className="text-xs text-mist">Stream URL or the station's website</span>
          <span className="flex gap-2">
            <input
              required
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setPicked(null);
                setFound([]);
                setInfo(null);
              }}
              className={inputCls}
              placeholder="https://example.com/stream or afc.fm"
              inputMode="url"
              autoCapitalize="off"
              spellCheck={false}
            />
            <button
              type="button"
              onClick={() => void find()}
              disabled={finding || !url.trim()}
              title="Look for the stream on this website"
              className="flex shrink-0 items-center gap-1.5 rounded-lg bg-white/5 px-3 text-xs font-medium text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
            >
              {finding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
              Find
            </button>
          </span>
        </label>

        {finding && <p className="text-xs text-mist">Searching the site and its player pages… (up to ~30 s)</p>}
        {info && !finding && <p className="text-xs text-[var(--accent)]">{info}</p>}

        {found.length > 1 && (
          <ul className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-white/10 bg-black/20 p-1">
            {found.map((c) => (
              <li key={c.url}>
                <button
                  type="button"
                  onClick={() => apply(c, homepage)}
                  className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${
                    picked === c.url ? "bg-white/15 text-white" : "text-mist hover:bg-white/5 hover:text-white"
                  }`}
                >
                  {picked === c.url ? <Check className="h-3.5 w-3.5 shrink-0" /> : <span className="w-3.5 shrink-0" />}
                  <span className="min-w-0 flex-1 truncate font-mono">{c.url}</span>
                  {(c.format || c.bitrate) && (
                    <span className="shrink-0 rounded bg-white/5 px-1.5 py-0.5 text-[10px]">
                      {[c.format, c.bitrate ? `${c.bitrate} kbps` : null].filter(Boolean).join(" · ")}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}

        <label className="block space-y-1">
          <span className="text-xs text-mist">Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} placeholder="Filled in by Find, or type one" />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block space-y-1">
            <span className="text-xs text-mist">Genre</span>
            <input value={genre} onChange={(e) => setGenre(e.target.value)} className={inputCls} placeholder="HOUSE, DEEP — separate several with commas" />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-mist">Category</span>
            <input value={category} onChange={(e) => setCategory(e.target.value)} className={inputCls} placeholder="Radio" />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={favorite} onChange={(e) => setFavorite(e.target.checked)} className="h-4 w-4 accent-[var(--accent)]" />
          Add to favorites
        </label>
        <p className="text-[11px] text-mist">The station's logo is looked up automatically once it is added.</p>
        {error && <p className="text-xs text-red-300">{error}</p>}
        <div className="flex gap-2 pt-1">
          <button type="button" onClick={close} className="flex-1 rounded-lg bg-white/5 py-2 text-sm text-mist hover:bg-white/10">
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || finding}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-[var(--accent)] py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy || finding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
            Add station
          </button>
        </div>
      </form>
    </Modal>
  );
}
