import {
  Activity,
  Disc3,
  Bluetooth,
  Pencil,
  Cpu,
  Download,
  KeyRound,
  Lightbulb,
  ListMusic,
  Loader2,
  Power,
  Radio,
  RotateCcw,
  SlidersHorizontal,
  Upload,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useSessionState } from "../hooks/useSessionState";
import type { DeviceConfig } from "../types";
import { LogoBatch } from "./LogoBatch";
import { StreamCheck } from "./StreamCheck";
import { SystemResources } from "./SystemResources";
import { WebLogin } from "./WebLogin";
import { Modal } from "./Modal";
import { StreamSettingsForm } from "./StreamSettingsForm";

interface Props {
  open: boolean;
  onClose: () => void;
}

// YYYYMMDD-HHMMSS from now. Avoids a bracketed regex char-class, which
// Tailwind's class scanner would otherwise mistake for an arbitrary property.
function nowStamp(): string {
  const d = new Date().toISOString().replace(/\D/g, "");
  return `${d.slice(0, 8)}-${d.slice(8, 14)}`;
}

/** Hands [blob] to the browser as a download. The object URL outlives the
 * click: revoking it on the spot can cancel the download in Safari/Firefox. */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

function Switch({ on, onClick, busy }: { on: boolean; onClick: () => void; busy?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={busy}
      onClick={onClick}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
        on ? "bg-[var(--accent)]" : "bg-white/10"
      }`}
    >
      <span className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-transform ${on ? "left-6" : "left-1"}`} />
    </button>
  );
}

const TABS = [
  { id: "general", label: "General", icon: SlidersHorizontal },
  { id: "streams", label: "Streams", icon: Activity },
  { id: "recording", label: "Recording", icon: Disc3 },
  { id: "bluetooth", label: "Bluetooth", icon: Bluetooth },
  { id: "hardware", label: "Hardware", icon: Cpu },
  { id: "sources", label: "Sources", icon: ListMusic },
  { id: "system", label: "System", icon: Power },
] as const;
type TabId = (typeof TABS)[number]["id"];

export function SettingsModal({ open, onClose }: Props) {
  const [tab, setTab] = useSessionState<TabId>("settings.tab", "general");
  const [cfg, setCfg] = useState<DeviceConfig | null>(null);
  const [ip, setIp] = useState<string | null>(null);
  const [linkType, setLinkType] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [diagBusy, setDiagBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [ledBright, setLedBright] = useState(50);
  const fileRef = useRef<HTMLInputElement>(null);
  const brightTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    if (cfg?.leds_brightness != null) setLedBright(Math.round(cfg.leds_brightness * 100));
  }, [cfg?.leds_brightness]);

  const commitBrightness = (v: number) => {
    setLedBright(v);
    clearTimeout(brightTimer.current);
    brightTimer.current = setTimeout(() => void patch({ leds_brightness: v / 100 }), 300);
  };

  useEffect(() => {
    if (!open) return;
    setMsg(null);
    api.getConfig().then(setCfg).catch(() => setMsg("Config unavailable"));
    api
      .networkStatus()
      .then((n) => {
        setIp(n.ip4 ?? null);
        setLinkType(n.link?.type ?? null);
      })
      .catch(() => {});
  }, [open]);

  const [jamendoKey, setJamendoKey] = useState("");
  const [hostDraft, setHostDraft] = useState<string | null>(null);
  const [hostMsg, setHostMsg] = useState<{ text: string; url?: string; error?: boolean } | null>(null);

  // Mirrors backend valid_hostname(): RFC 1123 label, lower case.
  const hostError = (n: string): string | null => {
    const v = n.trim().toLowerCase().replace(/\.local$/, "");
    if (!v) return "Enter a name";
    if (v.length > 63) return "At most 63 characters";
    if (!/^[a-z0-9-]+$/.test(v)) return "Only a-z, 0-9 and '-'";
    if (v.startsWith("-") || v.endsWith("-")) return "Cannot start or end with '-'";
    if (/^[0-9]+$/.test(v)) return "Not only digits";
    if (v === "localhost") return "'localhost' is reserved";
    return null;
  };

  const renameHost = async () => {
    if (hostDraft === null) return;
    const name = hostDraft.trim().toLowerCase().replace(/\.local$/, "");
    if (hostError(name) || name === cfg?.hostname) return;
    setBusy(true);
    setHostMsg(null);
    try {
      const r = await api.setHostname(name);
      setHostDraft(null);
      setHostMsg({ text: `Renamed. This box now answers at ${r.mdns}; the old name stops working.`, url: r.url });
      // Not awaited inside the try: the page may be on the old name, and a
      // failed refresh must not report a rename that succeeded as failed.
      api.getConfig().then(setCfg).catch(() => {});
    } catch (e) {
      setHostMsg({ text: e instanceof Error ? e.message : "Rename failed", error: true });
    } finally {
      setBusy(false);
    }
  };

  const patch = async (p: Parameters<typeof api.putConfig>[0]) => {
    setBusy(true);
    setMsg(null);
    try {
      setCfg(await api.putConfig(p));
      window.dispatchEvent(new Event("safydo:config-changed"));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const downloadDiag = async () => {
    setDiagBusy(true);
    try {
      const text = await api.diag();
      saveBlob(new Blob([text], { type: "text/plain" }), `safydo-diag-${nowStamp()}.txt`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Diagnostics failed");
    } finally {
      setDiagBusy(false);
    }
  };

  const backupStations = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const streams = await api.streams(false, true); // include disabled
      const version = cfg?.streams_version || nowStamp();
      saveBlob(
        new Blob([JSON.stringify({ streams }, null, 2)], { type: "application/json" }),
        `stations_${version}.json`
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Backup failed");
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!confirm(`Replace ALL stations with "${file.name}"? This overwrites the current list.`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const parsed = JSON.parse(await file.text());
      const arr = Array.isArray(parsed) ? parsed : parsed?.streams;
      if (!Array.isArray(arr)) throw new Error("Not a stations file ({\"streams\":[…]} or an array)");
      await api.replaceStreams(arr);
      // The station list on the page still shows the old stations otherwise.
      window.dispatchEvent(new Event("safydo:streams-changed"));
      setCfg(await api.getConfig());
      setMsg(`Replaced with ${arr.length} stations from ${file.name}`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : "Import failed");
    } finally {
      setBusy(false);
    }
  };

  const row = "flex items-center justify-between gap-3 py-2";

  return (
    <Modal open={open} onClose={onClose} title="Settings" large>
      <div className="flex flex-col gap-4 sm:flex-row sm:gap-6">
      <div
        role="tablist"
        aria-label="Settings sections"
        aria-orientation="vertical"
        className="-mx-1 flex shrink-0 gap-1 overflow-x-auto px-1 pb-1 sm:mx-0 sm:w-40 sm:flex-col sm:overflow-visible sm:px-0 sm:pb-0"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className={`flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium transition-colors sm:rounded-lg sm:py-2 sm:text-sm ${
              tab === t.id ? "bg-[var(--accent)] text-white" : "bg-white/5 text-mist hover:text-white"
            }`}
          >
            <t.icon className="h-3.5 w-3.5" /> {t.label}
          </button>
        ))}
      </div>
      {/* Two columns on a desktop-width modal: most tabs fit without scrolling. */}
      <div className="grid min-w-0 flex-1 content-start gap-x-8 gap-y-5 text-sm lg:grid-cols-2">
        {tab === "general" && (
          <>
        <section>
          <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Device</h3>
          <dl className="divide-y divide-white/5">
            <div className={row}>
              <dt className="text-mist">Hostname</dt>
              <dd className="flex min-w-0 items-center gap-1.5 font-medium">
                {hostDraft === null ? (
                  <>
                    {cfg?.hostname ?? "—"}
                    <button
                      type="button"
                      disabled={!cfg || busy}
                      onClick={() => {
                        setHostMsg(null);
                        setHostDraft(cfg?.hostname ?? "");
                      }}
                      className="rounded p-1 text-mist hover:bg-white/5 hover:text-white disabled:opacity-40"
                      aria-label="Rename"
                      title="Rename this box (its .local address changes)"
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                  </>
                ) : (
                  <form
                    className="flex items-center gap-1"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void renameHost();
                    }}
                  >
                    <input
                      autoFocus
                      value={hostDraft}
                      maxLength={69}
                      onChange={(e) => setHostDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key !== "Escape") return;
                        // Handled here: Modal leaves a prevented Escape alone, so
                        // this cancels the rename without closing Settings too.
                        e.preventDefault();
                        setHostDraft(null);
                      }}
                      aria-invalid={Boolean(hostError(hostDraft))}
                      className="w-32 rounded-lg border border-white/10 bg-black/20 px-2 py-1 text-xs outline-none focus:border-[var(--accent)]"
                    />
                    <span className="text-xs text-mist">.local</span>
                    <button
                      type="submit"
                      disabled={busy || Boolean(hostError(hostDraft))}
                      className="rounded-lg bg-[var(--accent)] px-2 py-1 text-xs text-white disabled:opacity-40"
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      onClick={() => setHostDraft(null)}
                      className="rounded-lg px-2 py-1 text-xs text-mist hover:text-white"
                    >
                      Cancel
                    </button>
                  </form>
                )}
              </dd>
            </div>
            {hostDraft !== null && hostError(hostDraft) && (
              <p className="py-1 text-right text-xs text-red-300">{hostError(hostDraft)}</p>
            )}
            {hostMsg && (
              <p className={`py-1 text-xs ${hostMsg.error ? "text-red-300" : "text-[var(--accent)]"}`}>
                {hostMsg.text}{" "}
                {hostMsg.url && (
                  <a href={hostMsg.url} className="underline">
                    Open {hostMsg.url}
                  </a>
                )}
              </p>
            )}
            <div className={row}>
              <dt className="text-mist">Address</dt>
              <dd className="font-medium">
                {cfg?.mdns ? `http://${cfg.mdns}` : "—"}
                {ip ? ` · ${ip}` : ""}
              </dd>
            </div>
            <div className={row}>
              <dt className="text-mist">Connection</dt>
              <dd className="font-medium capitalize">{linkType ?? "—"}</dd>
            </div>
          </dl>
        </section>

        <section>
          <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Playback</h3>
          <div className={row}>
            <div>
              <p className="font-medium">Resume on boot</p>
              <p className="text-xs text-mist">Restart the last stream/track after a reboot.</p>
            </div>
            <Switch on={Boolean(cfg?.resume_on_boot)} busy={busy} onClick={() => void patch({ resume_on_boot: !cfg?.resume_on_boot })} />
          </div>
          <div className={row}>
            <div>
              <p className="font-medium">Live rewind</p>
              <p className="text-xs text-mist">
                How far back you can scrub a live stream. Held in RAM, capped at 96 MB.
                {cfg?.rewind_kbps && cfg.rewind_bytes ? (
                  <>
                    {" "}Now: {(cfg.rewind_bytes / 1048576).toFixed(1)} MB at {cfg.rewind_kbps} kbps in RAM
                    {cfg.rewind_rate_source === "measured"
                      ? " (measured)"
                      : cfg.rewind_rate_source === "declared"
                        ? " (estimate; measured after 20 s)"
                        : ""}
                    {cfg.rewind_effective_minutes != null &&
                    cfg.rewind_minutes != null &&
                    cfg.rewind_effective_minutes < cfg.rewind_minutes - 0.5
                      ? ` — the cap holds ${cfg.rewind_effective_minutes} min of this stream`
                      : ""}
                    .
                  </>
                ) : (
                  " Sized from each stream's real bitrate while it plays."
                )}
              </p>
            </div>
            <select
              value={cfg?.rewind_minutes ?? 10}
              disabled={busy}
              onChange={(e) => void patch({ rewind_minutes: Number(e.target.value) })}
              className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
            >
              {[0, 5, 10, 20, 30, 45].map((m) => (
                <option key={m} value={m}>
                  {m ? `${m} min` : "Off"}
                </option>
              ))}
            </select>
          </div>
        </section>
          </>
        )}

        {tab === "general" && (
          <section>
            <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Modules</h3>
            {(
              [
                ["links", "Web links", "YouTube, SoundCloud, Mixcloud… panel and its weekly yt-dlp update."],
                ["files", "Files", "USB and network-drive browser."],
                ["recordings", "Recordings", "The ● record button and the Recordings browser."],
              ] as const
            ).map(([id, label, hint]) => (
              <div key={id} className={row}>
                <div>
                  <p className="font-medium">{label}</p>
                  <p className="text-xs text-mist">{hint}</p>
                </div>
                <Switch
                  on={cfg?.modules?.[id] ?? true}
                  busy={busy}
                  onClick={() => void patch({ modules: { [id]: !(cfg?.modules?.[id] ?? true) } })}
                />
              </div>
            ))}
            <p className="text-[10px] text-mist">
              Neither keeps anything running while idle, so switching one off mainly tidies the page.
            </p>
          </section>
        )}

        {tab === "streams" && <StreamSettingsForm row={row} />}

        {tab === "recording" && cfg?.recording && (
          <>
            <section>
              <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Format</h3>
              <div className="flex overflow-hidden rounded-lg border border-white/10">
                {(
                  [
                    ["mp3", "MP3"],
                    ["opus", "Opus"],
                    ["flac", "FLAC"],
                    ["original", "Original"],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    disabled={busy}
                    onClick={() => void patch({ recording: { format: id } })}
                    className={`flex-1 px-3 py-1.5 text-xs transition-colors ${
                      cfg.recording?.format === id ? "bg-[var(--accent)] text-white" : "text-mist hover:bg-white/5"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-mist">
                {
                  {
                    mp3: "Plays everywhere. Re-encoded from the source.",
                    opus: "Best quality per MB; most modern players handle it.",
                    flac: "Lossless copy of what the Pi decodes — big, but nothing is lost on top of the source.",
                    original: "The station's own stream, untouched (no re-encode, .mka). FM has no compressed original, so it comes out as raw PCM in .mka — use FLAC for FM.",
                  }[cfg.recording.format]
                }
              </p>
              {cfg.recording.format === "mp3" && (
                <div className={row}>
                  <p className="font-medium">Bitrate</p>
                  <select
                    value={cfg.recording.mp3_bitrate}
                    disabled={busy}
                    onChange={(e) => void patch({ recording: { mp3_bitrate: Number(e.target.value) } })}
                    className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                  >
                    {[128, 160, 192, 256, 320].map((b) => (
                      <option key={b} value={b}>
                        {b} kbps · {Math.round((b * 3600) / 8 / 1024)} MB/h
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {cfg.recording.format === "opus" && (
                <div className={row}>
                  <p className="font-medium">Bitrate</p>
                  <select
                    value={cfg.recording.opus_bitrate}
                    disabled={busy}
                    onChange={(e) => void patch({ recording: { opus_bitrate: Number(e.target.value) } })}
                    className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                  >
                    {[64, 96, 128, 160, 192, 256].map((b) => (
                      <option key={b} value={b}>
                        {b} kbps · {Math.round((b * 3600) / 8 / 1024)} MB/h
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {cfg.recording.format === "flac" && (
                <div className={row}>
                  <div>
                    <p className="font-medium">Compression level</p>
                    <p className="text-xs text-mist">Same sound at every level; higher is smaller and slower. ~300–500 MB/h.</p>
                  </div>
                  <select
                    value={cfg.recording.flac_level}
                    disabled={busy}
                    onChange={(e) => void patch({ recording: { flac_level: Number(e.target.value) } })}
                    className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                  >
                    {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((l) => (
                      <option key={l} value={l}>
                        {l}
                        {l === 5 ? " (default)" : l === 0 ? " (fastest)" : l === 8 ? " (smallest)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </section>

            <section>
              <h3 className="mb-1 font-display text-xs uppercase tracking-[0.2em] text-mist">Where &amp; how long</h3>
              <div className={row}>
                <div>
                  <p className="font-medium">Save to</p>
                  <p className="text-xs text-mist">
                    Auto uses the USB drive when one is mounted ("SAFYDO Recordings" folder), else the SD card.
                  </p>
                </div>
                <select
                  value={cfg.recording.location}
                  disabled={busy}
                  onChange={(e) => void patch({ recording: { location: e.target.value as "auto" | "usb" | "sd" } })}
                  className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                >
                  <option value="auto">Auto</option>
                  <option value="usb">USB drive</option>
                  <option value="sd">SD card</option>
                </select>
              </div>
              <div className={row}>
                <div>
                  <p className="font-medium">Stop by itself after</p>
                  <p className="text-xs text-mist">A forgotten recording should not fill the card. It also stops when you change station.</p>
                </div>
                <select
                  value={cfg.recording.max_minutes}
                  disabled={busy}
                  onChange={(e) => void patch({ recording: { max_minutes: Number(e.target.value) } })}
                  className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                >
                  {[30, 60, 120, 240, 480, 0].map((m) => (
                    <option key={m} value={m}>
                      {m ? (m >= 60 ? `${m / 60} h` : `${m} min`) : "No limit"}
                    </option>
                  ))}
                </select>
              </div>
            </section>
          </>
        )}

        {tab === "bluetooth" && (
          <>
        <section>
          <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <Bluetooth className="h-3.5 w-3.5" /> Bluetooth
          </h3>
          <div className={row}>
            <div>
              <p className="font-medium">Auto-connect</p>
              <p className="text-xs text-mist">Which known speaker to grab on boot.</p>
            </div>
            <div className="flex overflow-hidden rounded-lg border border-white/10">
              {(["strongest", "primary"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  disabled={busy}
                  onClick={() => void patch({ autoconnect_strategy: s })}
                  className={`px-3 py-1.5 text-xs capitalize transition-colors ${
                    cfg?.autoconnect_strategy === s ? "bg-[var(--accent)] text-white" : "text-mist hover:bg-white/5"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
          <div className={row}>
            <div>
              <p className="font-medium">Reboot if the radio wedges</p>
              <p className="text-xs text-mist">
                {cfg?.wedge_rebind_viable === "yes"
                  ? "Rarely needed here: this board's controller can be recovered without a reboot."
                  : cfg?.wedge_rebind_viable === "no"
                    ? "This board's controller can only be recovered by a reboot. Off means audio stays silent until you reboot it yourself."
                    : "A wedged controller may need a reboot to come back. Off means audio stays silent until you reboot it yourself."}
                {" Capped at 3 per day."}
              </p>
            </div>
            <Switch
              on={Boolean(cfg?.wedge_reboot)}
              busy={busy}
              onClick={() => void patch({ wedge_reboot: !cfg?.wedge_reboot })}
            />
          </div>
        </section>
          </>
        )}

        {tab === "hardware" && (
          <>
        {cfg?.radio_state && cfg.radio_state !== "unsupported" && (
          <section>
            <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
              <Radio className="h-3.5 w-3.5" /> FM radio
            </h3>
            <div className={row}>
              <div>
                <p className="font-medium">DABBoard tuner</p>
                <p className="text-xs text-mist">
                  Takes a reboot to switch on or off. Uses GPIO 23, so it replaces the Blinkt LEDs.
                </p>
              </div>
              <Switch
                on={cfg.radio_state !== "disabled"}
                busy={busy}
                onClick={() => void patch({ radio_enabled: cfg.radio_state === "disabled" })}
              />
            </div>
            {cfg.radio_state === "setup_needed" && (
              <p className="py-2 text-xs text-mist">
                The vendor tools aren't installed. Over SSH, run{" "}
                <code>sudo /opt/rpi-audio-player/scripts/radio_setup.sh</code> and follow what it says.
              </p>
            )}
            {cfg.radio_state === "not_detected" && (
              <p className="py-2 text-xs text-red-300">
                The board did not answer, so the FM radio panel is hidden. With the power off, check that it
                is seated on the header; it is tried again when the box starts.
              </p>
            )}
            {cfg.radio_state === "reboot_required" && (
              <div className={row}>
                <p className="text-xs text-mist">Reboot to apply this.</p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void api
                      .reboot()
                      .then(() => setMsg("Rebooting…"))
                      .catch((e) => setMsg(e instanceof Error ? e.message : "Reboot failed"))
                  }
                  className="rounded-full bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
                >
                  Reboot now
                </button>
              </div>
            )}
            {(cfg.radio_state === "ready" || cfg.radio_state === "playing") && (
              <>
                <div className={row}>
                  <div>
                    <p className="font-medium">Start radio after a reboot</p>
                    <p className="text-xs text-mist">Off leaves the tuner in reset until you press Listen.</p>
                  </div>
                  <Switch
                    on={Boolean(cfg.radio_resume_on_boot)}
                    busy={busy}
                    onClick={() => void patch({ radio_resume_on_boot: !cfg.radio_resume_on_boot })}
                  />
                </div>
                <div className={row}>
                  <div>
                    <p className="font-medium">RDS standard</p>
                    <p className="text-xs text-mist">
                      RBDS is the North American one. Applies the next time the tuner starts.
                    </p>
                  </div>
                  <select
                    value={cfg.radio_rds_region ?? "E"}
                    disabled={busy}
                    onChange={(e) => void patch({ radio_rds_region: e.target.value as "E" | "U" })}
                    className="rounded-lg bg-black/30 px-2 py-1.5 text-xs"
                  >
                    <option value="E">RDS (Europe)</option>
                    <option value="U">RBDS (N. America)</option>
                  </select>
                </div>
                <p className="py-2 text-xs text-mist">
                  While listening, don't unplug the box or swap USB adapters: a sudden power change can
                  damage the tuner. Shut down from here first.
                </p>
              </>
            )}
          </section>
        )}

        {!cfg?.radio_enabled && <section>
          <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <Lightbulb className="h-3.5 w-3.5" /> LED meter
          </h3>
          <div className={row}>
            <div>
              <p className="font-medium">Blinkt fitted</p>
              <p className="text-xs text-mist">
                A strip can't be detected, so say whether one is on the header.
              </p>
            </div>
            <Switch
              on={Boolean(cfg?.leds_fitted)}
              busy={busy}
              onClick={() => void patch({ leds_fitted: !cfg?.leds_fitted })}
            />
          </div>
          {!cfg?.leds_fitted ? null : cfg?.leds_available || cfg?.leds_enabled ? (
            <>
              {cfg?.leds_error && <p className="py-2 text-xs text-mist">{cfg.leds_error}</p>}
              <div className={row}>
                <div>
                  <p className="font-medium">Blinkt VU meter</p>
                  <p className="text-xs text-mist">Mirror the stereo meter on the 8 LEDs.</p>
                </div>
                <Switch on={Boolean(cfg?.leds_enabled)} busy={busy} onClick={() => void patch({ leds_enabled: !cfg?.leds_enabled })} />
              </div>
              <label className={`flex items-center gap-3 py-2 ${cfg?.leds_enabled ? "" : "opacity-40"}`}>
                <span className="w-16 shrink-0 text-xs text-mist">Brightness</span>
                <input
                  type="range"
                  min={1}
                  max={100}
                  value={ledBright}
                  disabled={!cfg?.leds_enabled}
                  onChange={(e) => commitBrightness(Number(e.target.value))}
                  className="h-1.5 min-w-0 flex-1 cursor-pointer appearance-none rounded-full bg-white/10 accent-[var(--accent)]"
                />
                <span className="w-9 shrink-0 text-right text-xs font-medium">{ledBright}%</span>
              </label>
            </>
          ) : (
            <p className="py-2 text-xs text-mist">
              {cfg?.leds_error || "Blinkt LED driver unavailable (GPIO 23/24)."}
            </p>
          )}
        </section>}
          </>
        )}

        {tab === "sources" && (
          <>
        {cfg?.sources && cfg.sources.length > 0 && (
          <section>
            <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
              <Radio className="h-3.5 w-3.5" /> Sources (plugins)
            </h3>
            {cfg.sources.map((s) => (
              <div key={s.id} className={row}>
                <div className="min-w-0">
                  <p className="font-medium">{s.label}</p>
                  <p className="text-xs text-mist">
                    {s.needs_config && !s.configured
                      ? "Needs an API key below"
                      : s.id === "radiobrowser"
                        ? "Free internet-radio directory"
                        : "Creative-Commons music"}
                  </p>
                </div>
                <Switch on={s.enabled} busy={busy} onClick={() => void patch({ source_enabled: { [s.id]: !s.enabled } })} />
              </div>
            ))}
            {cfg.sources.some((s) => s.id === "jamendo") && (
              <div className="flex items-center gap-2 py-2">
                <span className="w-20 shrink-0 text-xs text-mist">Jamendo key</span>
                <input
                  value={jamendoKey}
                  onChange={(e) => setJamendoKey(e.target.value)}
                  onBlur={() => jamendoKey.trim() && void patch({ jamendo_client_id: jamendoKey.trim() })}
                  placeholder={cfg.sources.find((s) => s.id === "jamendo")?.configured ? "•••• configured" : "client_id"}
                  autoComplete="off"
                  className="min-w-0 flex-1 rounded-lg border border-white/10 bg-black/20 px-3 py-1.5 text-xs outline-none focus:border-[var(--accent)]"
                />
              </div>
            )}
            <p className="text-[10px] text-mist">Free to use; each is off by default. Add more in backend/sources/.</p>
          </section>
        )}

        <section>
          <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <ListMusic className="h-3.5 w-3.5" /> Stations
          </h3>
          <div className={row}>
            <div>
              <p className="font-medium">{cfg?.streams_count ?? "—"} stations</p>
              <p className="text-xs text-mist">V.{cfg?.streams_version ?? "—"}</p>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void backupStations()}
                className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
              >
                <Download className="h-3.5 w-3.5" /> Backup
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => fileRef.current?.click()}
                className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
              >
                <Upload className="h-3.5 w-3.5" /> Replace
              </button>
            </div>
          </div>
          <LogoBatch row={row} />
          <StreamCheck row={row} />
          <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" onChange={(e) => void onFile(e)} />
        </section>
          </>
        )}

        {msg && <p className="text-xs text-[var(--accent)] lg:col-span-2">{msg}</p>}

        {tab === "system" && (
          <>
        <section>
          <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <Cpu className="h-3.5 w-3.5" /> Resources
          </h3>
          <SystemResources />
        </section>
        <section>
          <h3 className="mb-1 flex items-center gap-1.5 font-display text-xs uppercase tracking-[0.2em] text-mist">
            <KeyRound className="h-3.5 w-3.5" /> Web login
          </h3>
          <WebLogin row={row} />
        </section>
        <section className="flex gap-2 border-t border-white/5 pt-4">
          <button
            type="button"
            disabled={diagBusy}
            onClick={() => void downloadDiag()}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-white/5 py-2 text-xs text-mist hover:bg-white/10 hover:text-white disabled:opacity-50"
          >
            {diagBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            {diagBusy ? "Collecting…" : "Diagnostics"}
          </button>
          <button
            type="button"
            onClick={() => {
              if (!confirm("Reboot the player now? Playback stops for about a minute.")) return;
              api
                .reboot()
                .then(() => setMsg("Rebooting… the page reconnects when the player is back."))
                .catch((e) => alert(e instanceof Error ? e.message : "Reboot failed"));
            }}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-white/5 py-2 text-xs text-mist hover:bg-white/10 hover:text-white"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Reboot
          </button>
          <button
            type="button"
            onClick={() => {
              if (!confirm("Shut down the player now?")) return;
              api.shutdown().catch((e) => alert(e instanceof Error ? e.message : "Shutdown failed"));
            }}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-white/5 py-2 text-xs text-mist hover:bg-red-500/10 hover:text-red-300"
          >
            <Power className="h-3.5 w-3.5" />
            Shut down
          </button>
        </section>
          </>
        )}
      </div>
      </div>
    </Modal>
  );
}
