import { Cpu, HardDrive, MemoryStick, Thermometer } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { SystemResources as Res } from "../types";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function size(bytes: number): string {
  return bytes >= GB ? `${(bytes / GB).toFixed(1)} GB` : `${Math.round(bytes / MB)} MB`;
}

function uptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

/** Green → amber → red as a share of the limit gets used up. */
function tone(pct: number): string {
  return pct >= 90 ? "#f87171" : pct >= 70 ? "#fbbf24" : "var(--accent)";
}

function Meter({ icon, label, pct, detail }: { icon: ReactNode; label: string; pct: number | null; detail: string }) {
  const p = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="flex items-center gap-1.5 font-medium">
          {icon} {label}
        </span>
        <span className="text-mist">{detail}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
        <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${p}%`, background: tone(p) }} />
      </div>
    </div>
  );
}

/**
 * Settings → System. Read only while this tab is showing: mounted with the
 * tab, it polls every 3 s and stops when the tab or Settings closes.
 */
export function SystemResources() {
  const [res, setRes] = useState<Res | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const r = await api.systemResources();
        if (!cancelled) {
          setRes(r);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Unavailable");
      }
      if (!cancelled && document.visibilityState !== "hidden") timer = window.setTimeout(tick, 3000);
      else if (!cancelled) timer = window.setTimeout(tick, 15000);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  if (!res) {
    return <p className="py-2 text-xs text-mist">{error ?? "Reading the board…"}</p>;
  }

  const { cpu, memory, temperature_c: temp, throttle } = res;
  const memPct = memory.total ? (100 * memory.used) / memory.total : null;
  // The SoC starts throttling at 80 °C (soft limit 60 °C on some firmware); scale to 85.
  const tempPct = temp != null ? (100 * temp) / 85 : null;

  return (
    <div className="space-y-3 py-2">
      <Meter
        icon={<Cpu className="h-3.5 w-3.5 text-mist" />}
        label="CPU"
        pct={cpu.percent}
        detail={[
          cpu.percent != null ? `${cpu.percent.toFixed(0)} %` : null,
          cpu.freq_mhz ? `${cpu.freq_mhz}${cpu.freq_max_mhz ? `/${cpu.freq_max_mhz}` : ""} MHz` : null,
          cpu.load ? `load ${cpu.load.map((l) => l.toFixed(2)).join(" ")}` : null,
          cpu.cores ? `${cpu.cores} cores` : null,
        ]
          .filter(Boolean)
          .join(" · ")}
      />
      <Meter
        icon={<Thermometer className="h-3.5 w-3.5 text-mist" />}
        label="Temperature"
        pct={tempPct}
        detail={temp != null ? `${temp.toFixed(1)} °C` : "not available"}
      />
      <Meter
        icon={<MemoryStick className="h-3.5 w-3.5 text-mist" />}
        label="RAM"
        pct={memPct}
        detail={`${size(memory.used)} of ${size(memory.total)}${
          memory.backend_rss ? ` · backend ${size(memory.backend_rss)}` : ""
        }${memory.swap_total ? ` · swap ${size(memory.swap_used)}` : ""}`}
      />
      {res.disks.map((d) => (
        <Meter
          key={d.path}
          icon={<HardDrive className="h-3.5 w-3.5 text-mist" />}
          label={d.label}
          pct={d.total ? (100 * d.used) / d.total : null}
          detail={`${size(d.used)} of ${size(d.total)} · ${size(d.free)} free`}
        />
      ))}
      <p className="text-[11px] text-mist">
        {res.uptime_s != null && `Up ${uptime(res.uptime_s)}`}
        {throttle &&
          (throttle.under_voltage || throttle.throttled ? (
            <span className="ml-2 text-red-300">
              {throttle.under_voltage ? "Under-voltage now" : "Throttled now"} — check the power supply
            </span>
          ) : throttle.under_voltage_since_boot || throttle.throttled_since_boot ? (
            <span className="ml-2 text-amber-300">Power dipped or throttled since boot ({throttle.raw})</span>
          ) : (
            <span className="ml-2">· power OK</span>
          ))}
      </p>
    </div>
  );
}
