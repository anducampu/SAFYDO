import { Star } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { CollapseToggle } from "./CollapseToggle";
import { useCollapsed } from "../hooks/useCollapsed";
import { api } from "../api/client";
import type { SourceInfo } from "../types";
import { DiscoverBrowser } from "./DiscoverBrowser";
import { LocalStations } from "./LocalStations";

interface Props {
  onPlay: () => void;
}

export function StreamBrowser({ onPlay }: Props) {
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [tab, setTab] = useState("local");
  const [count, setCount] = useState(0);
  const [collapsed, toggleCollapsed] = useCollapsed("stations");

  useEffect(() => {
    const load = () => api.sources().then(setSources).catch(() => setSources([]));
    void load();
    // Re-read when a source is toggled in Settings (SettingsModal dispatches this).
    window.addEventListener("safydo:config-changed", load);
    return () => window.removeEventListener("safydo:config-changed", load);
  }, []);

  const enabled = useMemo(() => sources.filter((s) => s.enabled), [sources]);

  useEffect(() => {
    if (tab !== "local" && !enabled.some((s) => s.id === tab)) setTab("local");
  }, [enabled, tab]);

  const activeSource = enabled.find((s) => s.id === tab);
  const tabs = [{ id: "local", label: "My stations" }, ...enabled.map((s) => ({ id: s.id, label: s.label }))];

  return (
    <section className="glass rounded-2xl p-4" data-collapsed={collapsed}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-display text-lg font-semibold">
          <Star className="h-4 w-4 text-[var(--accent)]" /> Stations <span className="font-normal text-mist">({count})</span>
        </h2>
        <div className="ml-auto flex items-center gap-1">
          {!collapsed && enabled.length > 0 && (
            <div className="flex flex-wrap gap-0.5 rounded-full bg-black/20 p-0.5">
              {tabs.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setTab(t.id)}
                  className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                    tab === t.id ? "bg-[var(--accent)] text-white" : "text-mist hover:text-white"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}
          <CollapseToggle collapsed={collapsed} onToggle={toggleCollapsed} label="Stations" />
        </div>
      </div>

      {tab === "local" ? (
        <LocalStations onPlay={onPlay} onCount={setCount} />
      ) : (
        <DiscoverBrowser
          key={tab}
          sourceId={tab}
          configured={Boolean(activeSource?.configured)}
          needsConfig={Boolean(activeSource?.needs_config)}
          onPlay={onPlay}
          onCount={setCount}
        />
      )}
    </section>
  );
}
