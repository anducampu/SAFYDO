import { Cable, Globe, Loader2, Router, Trash2, Wifi } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { CollapseToggle } from "./CollapseToggle";
import { useCollapsed } from "../hooks/useCollapsed";
import { api } from "../api/client";
import type { NetworkStatus } from "../types";

// ≥60 % link quality streams comfortably; 40–59 % risks dropouts under
// BT/WiFi coexistence; below that expect stalls.
function signalTone(percent: number): string {
  if (percent >= 60) return "text-[var(--accent)]";
  if (percent >= 40) return "text-amber-200";
  return "text-red-300";
}

function WifiSignal({ percent, dbm }: { percent: number; dbm?: number | null }) {
  const level = percent >= 75 ? 4 : percent >= 55 ? 3 : percent >= 35 ? 2 : 1;
  return (
    <span className={`flex items-center gap-1.5 font-medium ${signalTone(percent)}`}>
      <span className="inline-flex items-end gap-[2px]">
        {[1, 2, 3, 4].map((i) => (
          <span
            key={i}
            className={`w-[3px] rounded-sm ${i <= level ? "bg-current" : "bg-white/15"}`}
            style={{ height: `${3 + i * 2}px` }}
          />
        ))}
      </span>
      {percent}%{dbm != null ? ` · ${dbm} dBm` : ""}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <dt className="text-mist">{label}</dt>
      <dd className="min-w-0 truncate text-right font-medium">{children}</dd>
    </div>
  );
}

export function NetworkPanel() {
  const [net, setNet] = useState<NetworkStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [collapsed, toggleCollapsed] = useCollapsed("network");
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);

  const load = useCallback(async () => {
    try {
      setNet(await api.networkStatus());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Network status failed");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 15000);
    return () => clearInterval(t);
  }, [load]);

  /** True when [fn] succeeded. */
  const run = async (fn: () => Promise<unknown>, okMsg?: string): Promise<boolean> => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      if (okMsg) setMsg({ text: okMsg });
      await load();
      return true;
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : "Request failed", error: true });
      return false;
    } finally {
      setBusy(false);
    }
  };

  // The password is kept when saving failed, so a retry needs no retyping.
  const addWifi = async () => {
    if (await run(() => api.networkAddWifi(ssid.trim(), password), `Saved “${ssid.trim()}” — reconnecting…`)) {
      setPassword("");
    }
  };

  const wired = net?.link?.type === "ethernet";
  const captive = net?.captive_portal;

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="flex items-center gap-2">
        {wired ? <Cable className="h-4 w-4 text-[var(--accent)]" /> : <Globe className="h-4 w-4 text-[var(--accent)]" />}
        <h2 className="font-display text-sm font-semibold">Network</h2>
        <span className="ml-auto flex items-center gap-1">
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-mist" />}
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Network" />
        </span>
      </div>

      {error && <p className="mt-2 text-xs text-red-300">{error}</p>}

      {net && wired ? (
        // Ethernet: show only wired config, no WiFi.
        <dl className="mt-3 space-y-1.5 text-xs">
          <Row label="Type">Wired · Ethernet</Row>
          <Row label="Interface">{net.link?.iface ?? "—"}</Row>
          <Row label="IP config">
            <span className={net.ip_method === "dhcp" ? "text-[var(--accent)]" : ""}>
              {net.ip_method === "dhcp" ? "DHCP (automatic)" : net.ip_method === "static" ? "Static" : "—"}
            </span>
          </Row>
          <Row label="IP address">{net.ip4 ?? "—"}</Row>
          <Row label="Gateway">{net.gateway ?? "—"}</Row>
          <Row label="DNS">{net.dns?.length ? net.dns.join(", ") : "—"}</Row>
          <Row label="Internet">
            <span className={net.online ? "text-[var(--accent)]" : "text-mist"}>{net.online ? "Online" : "Offline"}</span>
          </Row>
        </dl>
      ) : net ? (
        // WiFi view
        <>
          <dl className="mt-3 space-y-1.5 text-xs">
            <Row label="WiFi">{net.ssid ?? "—"}</Row>
            <Row label="Signal">
              {net.wifi_signal != null ? <WifiSignal percent={net.wifi_signal} dbm={net.wifi_dbm} /> : <span className="text-mist">—</span>}
            </Row>
            <Row label="IP">{net.ip4 ?? "—"}</Row>
            <Row label="Gateway">{net.gateway ?? "—"}</Row>
            <Row label="DNS">{net.dns?.length ? net.dns.join(", ") : "—"}</Row>
            <Row label="Setup AP">
              <span className={captive ? "text-[var(--accent)]" : "text-mist"}>{captive ? "Safydo active" : "Off"}</span>
            </Row>
          </dl>

          {net.saved_networks && net.saved_networks.length > 0 && (
            <div className="mt-3">
              <p className="text-xs text-mist">Saved networks</p>
              <ul className="mt-1 space-y-0.5 text-xs font-medium">
                {net.saved_networks.map((n) => (
                  <li key={n.id} className="flex items-center gap-1.5">
                    <Wifi className="h-3 w-3 shrink-0 text-mist" />
                    <span className="min-w-0 flex-1 truncate">{n.ssid}</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void run(() => api.networkForgetWifi(n.id), `Removed “${n.ssid}”`)}
                      className="shrink-0 rounded p-1 text-mist hover:bg-white/5 hover:text-red-300 disabled:opacity-40"
                      aria-label={`Forget ${n.ssid}`}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <form
            className="mt-4 space-y-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (ssid.trim()) void addWifi();
            }}
          >
            <input
              type="text"
              placeholder="WiFi name (SSID)"
              value={ssid}
              onChange={(e) => setSsid(e.target.value)}
              className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              autoComplete="off"
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              autoComplete="new-password"
            />
            <button
              type="submit"
              disabled={busy || !ssid.trim()}
              className="w-full rounded-xl bg-[var(--accent)]/20 py-2 text-sm font-medium text-[var(--accent)] transition hover:bg-[var(--accent)]/30 disabled:opacity-40"
            >
              Add WiFi network
            </button>
          </form>

          <div className="mt-3 flex gap-2">
            <button
              type="button"
              disabled={busy || captive}
              onClick={() => void run(() => api.networkCaptiveStart(), "Setup AP started — join Safydo")}
              className="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-white/10 py-2 text-xs transition hover:bg-white/5 disabled:opacity-40"
            >
              <Router className="h-3.5 w-3.5" />
              Start setup AP
            </button>
            <button
              type="button"
              disabled={busy || !captive}
              onClick={() => void run(() => api.networkCaptiveStop(), "Setup AP stopped")}
              className="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-white/10 py-2 text-xs transition hover:bg-white/5 disabled:opacity-40"
            >
              Stop setup AP
            </button>
          </div>

          {captive && (
            <p className="mt-2 text-xs text-mist">
              On your phone: join <strong className="text-white">Safydo</strong>, open{" "}
              <strong className="text-white">http://192.168.4.1/</strong>
            </p>
          )}
        </>
      ) : null}

      {msg && <p className={`mt-2 text-xs ${msg.error ? "text-red-300" : "text-[var(--accent)]"}`}>{msg.text}</p>}
    </section>
  );
}
