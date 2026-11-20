import {
  AlertTriangle,
  Bluetooth,
  Headphones,
  Loader2,
  Radio,
  RefreshCw,
  RotateCcw,
  Star,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { CollapseToggle } from "./CollapseToggle";
import { useCollapsed } from "../hooks/useCollapsed";
import { api } from "../api/client";
import type { BtDevice, BtLinkSignal, BtStatus as BtStatusType } from "../types";

// Audio-capable, named, or actively connected/primary devices are
// interesting; nameless BLE beacons/phones with random MACs are noise.
const isRelevant = (d: BtDevice) =>
  Boolean(d.audio || d.connected || d.primary || !d.unnamed);

const relTime = (iso?: string | null) => {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
};

// Link quality of the *connected* device, which is a different measurement from
// the RSSI in a scan result above: 0-255 from HCI_Read_Link_Quality, shown as a
// percentage because that is the only honest reading of a vendor-defined scale.
// The RSSI that comes with it is relative to the golden receive range, not dBm,
// so it lives in the tooltip with that said out loud.
function LinkQuality({ pct, rssi }: { pct: number; rssi?: number | null }) {
  const level = pct >= 90 ? 4 : pct >= 70 ? 3 : pct >= 50 ? 2 : 1;
  const tone = pct >= 80 ? "text-[var(--accent)]" : pct >= 50 ? "text-amber-200" : "text-red-300";
  const title =
    `Link quality ${Math.round((pct * 255) / 100)}/255` +
    (rssi != null ? ` · RSSI ${rssi} dB vs the golden receive range (not dBm)` : "");
  return (
    <span className={`flex items-center justify-end gap-1.5 font-medium ${tone}`} title={title}>
      <span className="inline-flex items-end gap-[2px]">
        {[1, 2, 3, 4].map((i) => (
          <span
            key={i}
            className={`w-[3px] rounded-sm ${i <= level ? "bg-current" : "bg-white/15"}`}
            style={{ height: `${3 + i * 2}px` }}
          />
        ))}
      </span>
      {pct}%
    </span>
  );
}

function SignalBars({ rssi }: { rssi?: number | null }) {
  if (rssi == null) return null;
  const level = rssi >= -60 ? 4 : rssi >= -70 ? 3 : rssi >= -80 ? 2 : 1;
  return (
    <span
      className="ml-1 inline-flex items-end gap-[2px] align-baseline"
      title={`${rssi} dBm`}
    >
      {[1, 2, 3, 4].map((i) => (
        <span
          key={i}
          className={`w-[3px] rounded-sm ${
            i <= level ? "bg-[var(--accent)]" : "bg-white/15"
          }`}
          style={{ height: `${3 + i * 2}px` }}
        />
      ))}
    </span>
  );
}

// The controller can stop answering the host while every layer above it still
// reports healthy, so "no audio" and "all fine" look identical from here. When
// the watchdog has given up, this is the only place a person finds out.
function WedgeNotice({ wd }: { wd: NonNullable<BtStatusType["watchdog"]> }) {
  if (!wd.wedged) return null;
  // Wording follows what is actually true of this board, not which of the two
  // fault verdicts happened to land this minute — those alternate on a
  // controller that faults intermittently, and a notice that rewrote itself
  // every 60s read as noise.
  const detail = (wd.verdict ?? "").includes("cap reached")
    ? "It has already been recovered by rebooting several times today; the box is staying up so the fault can be looked at."
    : wd.rebind_viable === "yes"
      ? "Recovery runs by itself and takes about 15 seconds. If this stays up, the controller is not coming back without a reboot."
      : wd.reboot_enabled
        ? "On this board only a reboot recovers it, and the box is allowed to do that by itself."
        : "On this board only a reboot recovers it, and automatic reboot is off. Settings → Bluetooth turns it on.";
  return (
    <div className="mt-3 flex gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-amber-100">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="space-y-0.5 text-xs">
        <p className="font-medium">Bluetooth controller stopped answering</p>
        <p className="text-amber-100/80">{detail}</p>
        {wd.at && <p className="text-amber-100/50">since {new Date(wd.at).toLocaleTimeString()}</p>}
        {/* The box often has no one at a console, and on this board a reboot is
            the only cure, so the notice offers it rather than pointing elsewhere. */}
        {wd.rebind_viable !== "yes" && !(wd.verdict ?? "").startsWith("rebooting") && (
          <RebootButton />
        )}
      </div>
    </div>
  );
}

function RebootButton() {
  const [state, setState] = useState<"idle" | "busy" | "sent">("idle");
  return (
    <button
      type="button"
      disabled={state !== "idle"}
      onClick={() => {
        if (!confirm("Reboot the player now? Playback stops for about a minute.")) return;
        setState("busy");
        api
          .reboot()
          .then(() => setState("sent"))
          .catch((e) => {
            setState("idle");
            alert(e instanceof Error ? e.message : "Reboot failed");
          });
      }}
      className="mt-1.5 inline-flex items-center gap-1.5 rounded-lg bg-amber-500/20 px-2.5 py-1 font-medium text-amber-50 hover:bg-amber-500/30 disabled:opacity-60"
    >
      {state === "busy" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
      {state === "sent" ? "Rebooting…" : "Reboot now"}
    </button>
  );
}

interface Props {
  bt: BtStatusType;
  primaryMac?: string;
  onRefresh: () => void;
}

export function BluetoothPanel({ bt, primaryMac, onRefresh }: Props) {
  const connected = Boolean(bt.connected);
  const [devices, setDevices] = useState<BtDevice[]>([]);
  const [busy, setBusy] = useState(false);
  const [collapsed, toggleCollapsed] = useCollapsed("bluetooth");
  // Link quality is read from the controller only while this panel is open and
  // the page is visible: each reading is two HCI commands to a radio that is
  // busy streaming, and this onboard controller wedges under load.
  const [signal, setSignal] = useState<BtLinkSignal | null>(null);
  useEffect(() => {
    if (!connected || collapsed) {
      setSignal(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      if (document.visibilityState === "visible") {
        try {
          const r = await api.btSignal();
          if (!cancelled) setSignal(r.signal);
        } catch {
          /* keep the last reading */
        }
      }
      if (!cancelled) timer = window.setTimeout(tick, 5000);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [connected, collapsed]);
  const [scanning, setScanning] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [confirmUnpair, setConfirmUnpair] = useState<string | null>(null);
  const unmountedRef = useRef(false);

  useEffect(() => {
    // Set on mount too: StrictMode (dev) unmounts and remounts once, and a
    // flag left at true made every scan quit after 2 s with its spinner stuck.
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  useEffect(() => {
    if (!confirmUnpair) return;
    const t = setTimeout(() => setConfirmUnpair(null), 3000);
    return () => clearTimeout(t);
  }, [confirmUnpair]);

  // Known = paired, always shown (ready to connect without a scan).
  // Nearby = everything else, from scan results.
  const known = devices.filter((d) => d.paired);
  const nearbyAll = devices.filter((d) => !d.paired);
  const nearby = (showAll ? nearbyAll : nearbyAll.filter(isRelevant)).sort(
    (a, b) => (b.rssi ?? -999) - (a.rssi ?? -999)
  );
  const hidden = nearbyAll.length - nearby.length;

  const loadDevices = useCallback(async () => {
    try {
      setDevices(await api.btDevices());
    } catch {
      /* list may fail if BT adapter is down */
    }
  }, []);

  useEffect(() => {
    void loadDevices();
  }, [loadDevices, primaryMac, connected]);

  const run = async (fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      if (okMsg) setMsg(okMsg);
      await loadDevices();
      onRefresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const scan = async () => {
    setScanning(true);
    setMsg("Scanning… put your speaker in pairing mode — it can take ~15 s to appear");
    try {
      // Classic BR/EDR speakers need ~8-16 s of inquiry before their name
      // resolves; a shorter window intermittently ends just before they show.
      const res = await api.btScanStart(28);
      const endAt = Date.now() + res.seconds * 1000;
      while (Date.now() < endAt) {
        await new Promise((r) => setTimeout(r, 2000));
        if (unmountedRef.current) return;
        await loadDevices();
      }
      const found = await api.btDevices();
      setDevices(found);
      const audio = found.filter((d) => d.audio && !d.paired).length;
      setMsg(
        `Scan done — ${found.filter((d) => !d.paired).length} nearby` +
          (audio ? `, ${audio} audio` : "")
      );
      onRefresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Scan failed");
    } finally {
      if (!unmountedRef.current) setScanning(false);
    }
  };

  const deviceRow = (d: BtDevice, actions: React.ReactNode) => (
    <li
      key={d.mac}
      className={`flex items-center gap-2 rounded-xl border border-white/5 bg-black/20 px-3 py-2 text-xs ${
        d.unnamed ? "opacity-60" : ""
      }`}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">
          {d.primary && (
            <Star className="mr-1 inline h-3 w-3 text-[var(--accent)]" />
          )}
          {d.audio && (
            <Headphones className="mr-1 inline h-3 w-3 text-[var(--accent)]" />
          )}
          {d.name}
          <SignalBars rssi={d.rssi} />
        </p>
        <p className="truncate text-mist">
          {d.mac}
          {d.connected && " · connected"}
          {!d.connected && d.paired && relTime(d.last_connected)
            ? ` · last connected ${relTime(d.last_connected)}`
            : !d.connected && d.paired
              ? " · paired"
              : ""}
        </p>
      </div>
      <div className="flex shrink-0 flex-col gap-1">{actions}</div>
    </li>
  );

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bluetooth
            className="h-4 w-4"
            style={{ color: connected ? "var(--accent)" : "#8b8798" }}
          />
          <h2 className="font-display text-sm font-semibold">Bluetooth</h2>
          {(busy || scanning) && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-mist" />
          )}
        </div>
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => void scan()}
            disabled={busy || scanning}
            className="rounded-lg p-2 text-mist transition-colors hover:bg-white/5 hover:text-white disabled:opacity-50"
            aria-label="Scan for nearby devices"
            title="Scan ~28 s for new devices to pair (fills the Nearby list)"
          >
            <Radio className={`h-4 w-4 ${scanning ? "animate-pulse" : ""}`} />
          </button>
          <button
            type="button"
            onClick={() => void run(() => api.btConnect(), "Reconnecting…")}
            disabled={busy}
            className="rounded-lg p-2 text-mist transition-colors hover:bg-white/5 hover:text-white disabled:opacity-50"
            aria-label="Reconnect speaker"
            title="Reconnect the primary speaker (use if audio dropped)"
          >
            <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />
          </button>
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Bluetooth" />
        </div>
      </div>

      {bt.watchdog && <WedgeNotice wd={bt.watchdog} />}

      <dl className="mt-3 space-y-1.5 text-xs">
        <div className="flex justify-between gap-2">
          <dt className="text-mist">Device</dt>
          <dd className="truncate font-medium">{bt.device ?? "—"}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-mist">Codec</dt>
          <dd className="font-medium">{bt.codec ?? "—"}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-mist">Bitrate</dt>
          <dd className="font-medium">{bt.bitrate ?? "—"}</dd>
        </div>
        {connected && signal?.quality_pct != null && (
          <div className="flex justify-between gap-2">
            <dt className="text-mist">Signal</dt>
            <dd className="font-medium">
              <LinkQuality pct={signal.quality_pct} rssi={signal.rssi_golden} />
            </dd>
          </div>
        )}
        <div className="flex justify-between gap-2">
          <dt className="text-mist">Status</dt>
          <dd className={connected ? "text-[var(--accent)]" : "text-mist"}>
            {connected ? "Connected" : "Disconnected"}
          </dd>
        </div>
        {bt.device_codecs && bt.device_codecs.length > 0 && (
          <div className="flex justify-between gap-2">
            <dt className="text-mist">Device codecs</dt>
            <dd className="flex flex-wrap justify-end gap-1">
              {bt.device_codecs.map((c) => {
                // "SBC-XQ" is the negotiated SBC in dual-channel HD mode
                const active = c === (bt.codec ?? "").replace(/-XQ$/, "");
                return (
                  <button
                    key={c}
                    type="button"
                    disabled={busy || active || !connected}
                    title={active ? "Active codec" : `Switch to ${c}`}
                    onClick={() =>
                      void run(
                        () => api.btSelectCodec(c),
                        `Switched to ${c}`
                      )
                    }
                    className={
                      active
                        ? "rounded-md bg-[var(--accent)]/25 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--accent)]"
                        : "rounded-md border border-white/10 px-1.5 py-0.5 text-[10px] hover:bg-white/10 disabled:opacity-40"
                    }
                  >
                    {c}
                  </button>
                );
              })}
            </dd>
          </div>
        )}
        <div className="flex justify-between gap-2">
          <dt className="text-mist">Quality</dt>
          <dd
            className={
              bt.is_best_quality ? "text-[var(--accent)]" : "text-amber-200"
            }
          >
            {bt.is_best_quality
              ? `Best available (${bt.codec ?? bt.best_codec ?? "—"})`
              : `Negotiated ${bt.codec ?? "—"} — best: ${bt.best_codec ?? "—"}`}
          </dd>
        </div>
        {connected && bt.xq_denied && bt.codec === "SBC" && (
          <div className="flex items-center justify-between gap-2">
            <dt className="text-mist">SBC-XQ</dt>
            <dd className="flex items-center gap-2 text-right text-[11px] text-amber-200">
              <span>Speaker couldn&apos;t keep up with XQ — standard SBC</span>
              <button
                type="button"
                disabled={busy}
                title="Try SBC-XQ again on this speaker"
                onClick={() =>
                  void run(() => api.btSelectCodec("SBC-XQ"), "Switched to SBC-XQ")
                }
                className="rounded-md border border-white/10 px-1.5 py-0.5 text-[10px] text-white hover:bg-white/10 disabled:opacity-40"
              >
                Retry XQ
              </button>
            </dd>
          </div>
        )}
      </dl>

      <h3 className="mt-4 text-[11px] font-semibold uppercase tracking-wide text-mist">
        My devices
      </h3>
      {known.length > 0 ? (
        <ul className="mt-1.5 space-y-1.5">
          {known.map((d) =>
            deviceRow(
              d,
              <>
                {(!d.connected || !d.primary) && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => api.btSetPrimary(d.mac),
                        `Connected: ${d.name}`
                      )
                    }
                    className="rounded-lg bg-[var(--accent)]/20 px-2 py-1 text-[10px] font-medium text-[var(--accent)] hover:bg-[var(--accent)]/30 disabled:opacity-40"
                  >
                    Connect
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (confirmUnpair === d.mac) {
                      setConfirmUnpair(null);
                      void run(
                        () => api.btUnpair(d.mac),
                        `Unpaired ${d.name}`
                      );
                    } else {
                      setConfirmUnpair(d.mac);
                    }
                  }}
                  className={
                    confirmUnpair === d.mac
                      ? "rounded-lg bg-amber-500/30 px-2 py-1 text-[10px] font-semibold text-amber-200 hover:bg-amber-500/40"
                      : "rounded-lg border border-white/10 px-2 py-1 text-[10px] text-amber-200/80 hover:bg-amber-500/10 disabled:opacity-40"
                  }
                >
                  {confirmUnpair === d.mac ? "Sure?" : "Unpair"}
                </button>
              </>
            )
          )}
        </ul>
      ) : (
        <p className="mt-1.5 text-xs text-mist">
          No paired devices — scan to pair a speaker.
        </p>
      )}

      {(nearby.length > 0 || scanning) && (
        <h3 className="mt-4 text-[11px] font-semibold uppercase tracking-wide text-mist">
          Nearby
        </h3>
      )}
      {nearby.length > 0 && (
        <ul className="mt-1.5 space-y-1.5">
          {nearby.map((d) =>
            deviceRow(
              d,
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(() => api.btPair(d.mac), `Paired ${d.name}`)
                }
                className="rounded-lg bg-[var(--accent)]/20 px-2 py-1 text-[10px] font-medium text-[var(--accent)] hover:bg-[var(--accent)]/30 disabled:opacity-40"
              >
                Pair
              </button>
            )
          )}
        </ul>
      )}

      {(hidden > 0 || showAll) && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="mt-2 text-[10px] text-mist underline-offset-2 hover:underline"
        >
          {showAll
            ? "Hide unnamed devices"
            : `Show ${hidden} unnamed device(s)`}
        </button>
      )}

      {msg && <p className="mt-2 text-xs text-[var(--accent)]">{msg}</p>}
    </section>
  );
}
