import { Code2, Eye, EyeOff, ImagePlus, Link2, Loader2, Plus, Save, Search, Star, Trash2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Stream } from "../types";
import { Modal } from "./Modal";
import { StationLogo } from "./StationLogo";
import { stationAccent } from "../lib/genreColors";

type Row = Stream & { __key: string };

interface Props {
  open: boolean;
  /** Edit only the favorites (the list's Favorites filter is on); the rest is kept as is on save. */
  favoritesOnly?: boolean;
  onClose: () => void;
  onSaved: () => void;
}

const inputCls =
  "w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-1.5 text-sm outline-none focus:border-[var(--accent)]";

const blank = (): Stream => ({
  id: "",
  name: "",
  url: "",
  genre: "",
  category: "Radio",
  favorite: false,
  disabled: false,
});

export function StreamEditor({ open, favoritesOnly = false, onClose, onSaved }: Props) {
  const [rows, setRows] = useState<Row[]>([]);
  const [filter, setFilter] = useState("");
  const [raw, setRaw] = useState(false);
  const [rawText, setRawText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Logo search runs against the saved station, so only rows that exist on the board can use it.
  const [savedIds, setSavedIds] = useState<Set<string>>(new Set());
  const [logoBusy, setLogoBusy] = useState<Record<string, boolean>>({});
  const [logoMsg, setLogoMsg] = useState<Record<string, string>>({});
  const [pasteFor, setPasteFor] = useState<string | null>(null);
  const [pasteUrl, setPasteUrl] = useState("");
  // Keys of the rows this session edits; null = the whole list. Fixed when the
  // editor opens, so un-starring a row here doesn't make it vanish mid-edit.
  const [scope, setScope] = useState<Set<string> | null>(null);
  // Save all replaces the whole list on the board, so it waits for this
  // session's own copy: an empty or leftover list must never be saved.
  const [loaded, setLoaded] = useState(false);
  const counter = useRef(0);
  const key = () => `k${counter.current++}`;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    setFilter("");
    setRaw(false);
    // Drop the last session's rows (Cancel keeps its discarded edits there).
    setLoaded(false);
    setRows([]);
    setScope(null);
    // all=true so disabled stations are editable here (they are hidden from the list).
    api
      .streams(false, true)
      .then((list) => {
        if (cancelled) return;
        setSavedIds(new Set(list.map((s) => s.id)));
        const fresh = list.map((s) => ({ ...s, __key: key() }));
        setRows(fresh);
        setScope(favoritesOnly ? new Set(fresh.filter((r) => r.favorite).map((r) => r.__key)) : null);
        setLoaded(true);
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Load failed"));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const strip = (r: Row): Stream => ({
    id: r.id,
    name: r.name,
    url: r.url,
    genre: r.genre,
    category: r.category,
    favorite: !!r.favorite,
    disabled: !!r.disabled,
    ...(r.homepage ? { homepage: r.homepage } : {}),
    ...(r.logo ? { logo: r.logo } : {}),
    ...(r.color ? { color: r.color } : {}),
  });

  const inScope = (r: Row) => !scope || scope.has(r.__key);

  /** The full list with the scoped rows replaced by [edited], in their original places. */
  const mergeScoped = (edited: Row[]): Row[] => {
    if (!scope) return edited;
    const out: Row[] = [];
    let i = 0;
    let lastSlot = -1;
    for (const r of rows) {
      if (!inScope(r)) {
        out.push(r);
      } else if (i < edited.length) {
        out.push(edited[i++]);
        lastSlot = out.length;
      }
    }
    out.splice(lastSlot < 0 ? 0 : lastSlot, 0, ...edited.slice(i));
    return out;
  };

  /** Searches the web (or takes [hint], a pasted image URL) and stores the logo right away. */
  const findLogo = async (r: Row, hint?: string) => {
    setLogoBusy((b) => ({ ...b, [r.__key]: true }));
    setLogoMsg((m) => ({ ...m, [r.__key]: "" }));
    try {
      const res = await api.findLogo(r.id, {
        url: r.url,
        name: r.name,
        homepage: r.homepage,
        ...(hint ? { hint, only_hint: true } : {}),
      });
      if (res.found) {
        update(r.__key, "logo", res.stream.logo);
        setPasteFor(null);
        setPasteUrl("");
      }
      setLogoMsg((m) => ({
        ...m,
        [r.__key]: res.found ? "" : hint ? "That address is not a usable image" : "No logo found",
      }));
    } catch (e) {
      setLogoMsg((m) => ({ ...m, [r.__key]: e instanceof Error ? e.message : "Logo search failed" }));
    } finally {
      setLogoBusy((b) => ({ ...b, [r.__key]: false }));
    }
  };

  /** A file (picked, pasted or dropped) or pasted SVG markup, stored as the logo right away. */
  const uploadLogo = async (r: Row, image: Blob | string) => {
    setLogoBusy((b) => ({ ...b, [r.__key]: true }));
    setLogoMsg((m) => ({ ...m, [r.__key]: "" }));
    try {
      const res = await api.uploadLogo(r.id, image);
      update(r.__key, "logo", res.stream.logo);
      setPasteFor(null);
      setPasteUrl("");
    } catch (e) {
      setLogoMsg((m) => ({ ...m, [r.__key]: e instanceof Error ? e.message : "Upload failed" }));
    } finally {
      setLogoBusy((b) => ({ ...b, [r.__key]: false }));
    }
  };

  const isSvgText = (t: string) => /^\s*(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*<svg[\s>]/i.test(t);

  function update<K extends keyof Stream>(k: string, field: K, value: Stream[K]) {
    setRows((rs) => rs.map((r) => (r.__key === k ? { ...r, [field]: value } : r)));
  }
  const remove = (k: string) => setRows((rs) => rs.filter((r) => r.__key !== k));
  const addRow = () => {
    const k = key();
    setRows((rs) => [{ ...blank(), favorite: favoritesOnly, __key: k }, ...rs]);
    setScope((sc) => (sc ? new Set(sc).add(k) : sc));
  };

  const toRaw = () => {
    setRawText(JSON.stringify(rows.filter(inScope).map(strip), null, 2));
    setRaw(true);
    setError(null);
  };
  const fromRaw = () => {
    try {
      const parsed = JSON.parse(rawText);
      if (!Array.isArray(parsed)) throw new Error("JSON must be an array of stations");
      const edited = parsed.map((s: Partial<Stream>) => ({ ...blank(), ...s, __key: key() }));
      setRows(mergeScoped(edited));
      if (scope) setScope(new Set(edited.map((r: Row) => r.__key)));
      setRaw(false);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON");
    }
  };

  const save = async () => {
    if (!loaded) return;
    let payload: Stream[];
    if (raw) {
      try {
        const parsed = JSON.parse(rawText);
        if (!Array.isArray(parsed)) throw new Error("JSON must be an array of stations");
        payload = mergeScoped(parsed.map((s: Partial<Stream>) => ({ ...blank(), ...s, __key: "" }))).map(strip);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Invalid JSON");
        return;
      }
    } else {
      payload = rows.map(strip);
    }
    setBusy(true);
    setError(null);
    try {
      const saved = await api.replaceStreams(payload);
      setSavedIds(new Set(saved.map((s) => s.id)));
      setRows(saved.map((s) => ({ ...s, __key: key() })));
      setScope(null);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const q = filter.trim().toLowerCase();
  const scoped = rows.filter(inScope);
  const visible = q
    ? scoped.filter((r) => r.name.toLowerCase().includes(q) || (r.genre || "").toLowerCase().includes(q))
    : scoped;
  const disabledCount = scoped.filter((r) => r.disabled).length;

  const footer = (
    <div className="flex gap-2">
      <button type="button" onClick={onClose} className="flex-1 rounded-lg bg-white/5 py-2.5 text-sm text-mist hover:bg-white/10">
        Cancel
      </button>
      <button
        type="button"
        onClick={() => void save()}
        disabled={busy || !loaded}
        title={loaded ? undefined : "Waiting for the station list"}
        className="flex flex-[2] items-center justify-center gap-2 rounded-lg bg-[var(--accent)] py-2.5 text-sm font-medium text-white disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save all
      </button>
    </div>
  );

  return (
    <Modal open={open} onClose={onClose} title={favoritesOnly ? "Edit stations (Favorites only)" : "Edit stations"} wide footer={footer}>
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={addRow}
            className="flex items-center gap-1 rounded-lg bg-white/5 px-2.5 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white"
          >
            <Plus className="h-3.5 w-3.5" /> Add row
          </button>
          <button
            type="button"
            onClick={() => (raw ? fromRaw() : toRaw())}
            className="flex items-center gap-1 rounded-lg bg-white/5 px-2.5 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white"
          >
            <Code2 className="h-3.5 w-3.5" /> {raw ? "Visual editor" : "Raw JSON"}
          </button>
          <span className="text-xs text-mist">
            {scoped.length} {scope ? (scoped.length === 1 ? "favorite" : "favorites") : "stations"}
            {scope ? ` of ${rows.length}` : ""}
            {disabledCount ? ` · ${disabledCount} hidden` : ""}
          </span>
          {!raw && (
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter…"
              className="ml-auto w-32 rounded-lg border border-white/10 bg-black/20 px-2.5 py-1.5 text-xs outline-none focus:border-[var(--accent)]"
            />
          )}
        </div>

        {error && <p className="rounded-lg bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</p>}

        {raw ? (
          <textarea
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            spellCheck={false}
            className="h-[52vh] w-full rounded-lg border border-white/10 bg-black/30 p-3 font-mono text-xs leading-relaxed outline-none focus:border-[var(--accent)]"
          />
        ) : (
          <ul className="space-y-2">
            {visible.map((r) => (
              <li
                key={r.__key}
                className={`rounded-xl border border-white/10 bg-black/20 p-2.5 ${r.disabled ? "opacity-55" : ""}`}
              >
                <div className="mb-1.5 flex items-center gap-2">
                  <StationLogo logo={r.logo} genre={r.genre} color={r.color} className="h-8 w-8 rounded text-[10px]" />
                  <label
                    className="relative h-6 w-6 shrink-0 cursor-pointer overflow-hidden rounded-full ring-1 ring-white/20"
                    style={{ background: stationAccent(r) }}
                    title={r.color ? "Station colour (click to change)" : "Colour from the genre — click to pick one"}
                  >
                    <input
                      type="color"
                      value={stationAccent(r)}
                      onChange={(e) => update(r.__key, "color", e.target.value)}
                      className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
                      aria-label="Station colour"
                    />
                  </label>
                  {r.color && (
                    <button
                      type="button"
                      onClick={() => update(r.__key, "color", undefined)}
                      className="-ml-1 shrink-0 rounded p-0.5 text-mist hover:text-white"
                      title="Back to the genre colour"
                      aria-label="Reset station colour"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                  <input
                    value={r.name}
                    onChange={(e) => update(r.__key, "name", e.target.value)}
                    placeholder="Name"
                    className={inputCls}
                  />
                  <button
                    type="button"
                    onClick={() => update(r.__key, "disabled", !r.disabled)}
                    className="shrink-0 rounded p-1 hover:bg-white/5"
                    aria-label={r.disabled ? "Show in list" : "Hide from list"}
                    title={r.disabled ? "Hidden from the list — click to show" : "Shown in the list — click to hide"}
                  >
                    {r.disabled ? <EyeOff className="h-4 w-4 text-amber-300" /> : <Eye className="h-4 w-4 text-mist" />}
                  </button>
                  <button
                    type="button"
                    onClick={() => update(r.__key, "favorite", !r.favorite)}
                    className="shrink-0 rounded p-1 hover:bg-white/5"
                    aria-label="Toggle favorite"
                  >
                    <Star className={`h-4 w-4 ${r.favorite ? "fill-[var(--accent)] text-[var(--accent)]" : "text-mist"}`} />
                  </button>
                  <button
                    type="button"
                    onClick={() => remove(r.__key)}
                    className="shrink-0 rounded p-1 text-mist hover:bg-white/5 hover:text-red-300"
                    aria-label="Delete station"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
                <input
                  value={r.url}
                  onChange={(e) => update(r.__key, "url", e.target.value)}
                  placeholder="https://…"
                  className={`${inputCls} mb-1.5`}
                />
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  <input
                    value={r.homepage ?? ""}
                    onChange={(e) => update(r.__key, "homepage", e.target.value)}
                    placeholder="Website (for the logo)"
                    className={`${inputCls} col-span-2 sm:col-span-1`}
                    inputMode="url"
                  />
                  <input
                    value={r.genre}
                    onChange={(e) => update(r.__key, "genre", e.target.value)}
                    placeholder="Genres, comma-separated"
                    className={inputCls}
                  />
                  <input
                    value={r.category}
                    onChange={(e) => update(r.__key, "category", e.target.value)}
                    placeholder="Category"
                    className={inputCls}
                  />
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px]">
                  {savedIds.has(r.id) ? (
                    <>
                      <button
                        type="button"
                        onClick={() => void findLogo(r)}
                        disabled={logoBusy[r.__key]}
                        className="flex items-center gap-1 rounded-md bg-white/5 px-2 py-1 text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
                        title="Search the station's website and the Radio Browser directory"
                      >
                        {logoBusy[r.__key] ? <Loader2 className="h-3 w-3 animate-spin" /> : <Search className="h-3 w-3" />}
                        {r.logo ? "Find another logo" : "Find logo"}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setPasteFor(pasteFor === r.__key ? null : r.__key);
                          setPasteUrl("");
                        }}
                        className="flex items-center gap-1 rounded-md bg-white/5 px-2 py-1 text-mist hover:bg-white/10 hover:text-white"
                        title="Paste an image address, a copied image file, or SVG code"
                      >
                        <Link2 className="h-3 w-3" /> Paste / URL
                      </button>
                      <label
                        className={`flex cursor-pointer items-center gap-1 rounded-md bg-white/5 px-2 py-1 text-mist hover:bg-white/10 hover:text-white ${
                          logoBusy[r.__key] ? "pointer-events-none opacity-50" : ""
                        }`}
                        title="Upload a logo file: PNG, JPEG, GIF, WebP, ICO or SVG"
                      >
                        <Upload className="h-3 w-3" /> Upload
                        <input
                          type="file"
                          accept="image/png,image/jpeg,image/gif,image/webp,image/x-icon,image/svg+xml,.svg,.ico"
                          className="hidden"
                          onChange={(e) => {
                            const f = e.target.files?.[0];
                            e.target.value = "";
                            if (f) void uploadLogo(r, f);
                          }}
                        />
                      </label>
                      {r.logo && (
                        <button
                          type="button"
                          onClick={() => update(r.__key, "logo", undefined)}
                          className="flex items-center gap-1 rounded-md bg-white/5 px-2 py-1 text-mist hover:bg-white/10 hover:text-red-300"
                          title="Remove the logo (on Save all)"
                        >
                          <X className="h-3 w-3" /> Remove logo
                        </button>
                      )}
                    </>
                  ) : (
                    <span className="flex items-center gap-1 text-mist">
                      <ImagePlus className="h-3 w-3" /> Save first to look up a logo
                    </span>
                  )}
                  {logoMsg[r.__key] && <span className="text-amber-300">{logoMsg[r.__key]}</span>}
                </div>
                {pasteFor === r.__key && (
                  <form
                    className="mt-1.5 flex gap-1.5"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const t = pasteUrl.trim();
                      if (!t) return;
                      if (isSvgText(t)) void uploadLogo(r, t);
                      else void findLogo(r, t);
                    }}
                  >
                    <input
                      autoFocus
                      value={pasteUrl}
                      onChange={(e) => setPasteUrl(e.target.value)}
                      onPaste={(e) => {
                        // A copied image file (or a screenshot) goes straight up;
                        // pasted SVG code too. A URL is left in the field to confirm.
                        const file = Array.from(e.clipboardData.files).find((f) => f.type.startsWith("image/") || /\.svg$/i.test(f.name));
                        if (file) {
                          e.preventDefault();
                          void uploadLogo(r, file);
                          return;
                        }
                        const text = e.clipboardData.getData("text/plain");
                        if (isSvgText(text)) {
                          e.preventDefault();
                          void uploadLogo(r, text);
                        }
                      }}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        const file = e.dataTransfer.files[0];
                        if (file) {
                          e.preventDefault();
                          void uploadLogo(r, file);
                        }
                      }}
                      placeholder="Image URL (.png, .svg…), paste a file or SVG code"
                      inputMode="url"
                      className={inputCls}
                    />
                    <button
                      type="submit"
                      disabled={logoBusy[r.__key] || !pasteUrl.trim()}
                      className="shrink-0 rounded-lg bg-[var(--accent)] px-3 text-xs font-medium text-white disabled:opacity-50"
                    >
                      Use
                    </button>
                  </form>
                )}
              </li>
            ))}
            {visible.length === 0 && <li className="py-6 text-center text-xs text-mist">No stations match.</li>}
          </ul>
        )}
      </div>
    </Modal>
  );
}
