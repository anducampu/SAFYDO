import { Github, Globe, Settings, Wifi, WifiOff } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api/client";
import { AudioCheckBanner } from "./components/AudioCheckBanner";
import { BluetoothPanel } from "./components/BluetoothPanel";
import { NetworkPanel } from "./components/NetworkPanel";
import { EqPanel } from "./components/EqPanel";
import { FilesBrowser } from "./components/FilesBrowser";
import { NowPlaying } from "./components/NowPlaying";
import { RadioPanel } from "./components/RadioPanel";
import { SettingsModal } from "./components/SettingsModal";
import { StreamBrowser } from "./components/StreamBrowser";
import { LinksPanel } from "./components/LinksPanel";
import { RecordingsPanel } from "./components/RecordingsPanel";
import { TransportControls } from "./components/TransportControls";
import { VolumeSlider } from "./components/VolumeSlider";
import { usePlayerStatus } from "./hooks/usePlayerStatus";

export default function App() {
  const { status, connected, error, refresh, bandsRef } = usePlayerStatus();
  const [showSettings, setShowSettings] = useState(false);
  const player = status?.player;
  const eqEnabled = Boolean(player?.eq.enabled);
  // Optional panels (Settings -> General -> Modules); shown until told otherwise.
  const [modules, setModules] = useState({ links: true, files: true, recordings: true });
  useEffect(() => {
    const load = () =>
      api
        .getConfig()
        .then((c) => c.modules && setModules(c.modules))
        .catch(() => {});
    void load();
    window.addEventListener("safydo:config-changed", load);
    return () => window.removeEventListener("safydo:config-changed", load);
  }, []);

  return (
    <div className="min-h-dvh bg-ink">
      {/* ambient glow */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-x-0 top-0 h-64"
        style={{
          background:
            "radial-gradient(60% 100% at 50% 0%, var(--accent-glow) 0%, transparent 70%)",
        }}
      />
      <div className="relative mx-auto max-w-6xl px-4 pb-10 pt-5 sm:px-6 lg:px-8">
        <header className="mb-5 flex items-center justify-between">
          <div>
            <p className="font-display text-[10px] uppercase tracking-[0.3em] text-mist">
              {status?.player ? "safydo.local" : "connecting…"}
            </p>
            <h1 className="font-display text-xl font-bold tracking-tight">Safydo</h1>
          </div>
          <div className="flex items-center gap-2">
            <span
              className={`flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-medium ${
                connected ? "bg-white/5 text-[var(--accent)]" : "bg-white/5 text-mist"
              }`}
            >
              {connected ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5" />}
              {connected ? "Live" : "Reconnecting"}
            </span>
            <button
              type="button"
              onClick={() => setShowSettings(true)}
              className="rounded-full bg-white/5 p-2 text-mist transition-colors hover:text-white"
              aria-label="Settings"
            >
              <Settings className="h-4 w-4" />
            </button>
          </div>
        </header>

        {error && (
          <p className="mb-4 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm text-red-200">
            {error}
          </p>
        )}

        <AudioCheckBanner check={status?.audio_check} />

        {/* Responsive layout: single column on phones, two-column (2fr / 1fr)
            from lg up. Each column is its own vertical stack, so cards never
            leave cross-row gaps regardless of height. */}
        <main className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-2">
            <NowPlaying status={status} connected={connected} bandsRef={bandsRef} />

            <div className="grid gap-4 sm:grid-cols-2">
              <TransportControls
                mpv={player?.mpv}
                live={Boolean(status?.radio?.listening)}
                onAction={refresh}
                recording={player?.recording}
                canRecord={modules.recordings}
                recordable={Boolean(status?.radio?.listening || player?.live)}
              />
              <VolumeSlider
                volume={Math.round(player?.mpv.volume ?? player?.volume_config ?? 80)}
                hwVolume={player?.hw_volume}
                followHardware={Boolean(player?.volume_follow_hardware)}
                speakerIgnoresVolume={Boolean(player?.speaker_ignores_volume)}
                speakerNoRemoteVolume={player?.speaker_remote_volume === false}
                volumeConfig={player?.volume_config}
                onChange={refresh}
              />
            </div>

            {/* Only with a tuner that is switched on AND answering. Every other
                state (off, no board, tools missing, reboot needed, board silent)
                is explained under Settings -> Hardware instead. */}
            {(status?.radio?.state === "ready" || status?.radio?.state === "playing") && (
              <RadioPanel onChange={refresh} />
            )}
            <StreamBrowser onPlay={refresh} />
            {modules.links && <LinksPanel
              onPlay={refresh}
              currentId={
                status?.player.saved.source === "link" ? status.player.saved.meta?.link_id : undefined
              }
              playlistId={
                status?.player.saved.source === "link_playlist"
                  ? status.player.saved.meta?.playlist_id
                  : undefined
              }
              playlistPos={status?.player.mpv.playlist_pos}
            />}
            {modules.files && <FilesBrowser onPlay={refresh} />}
            {modules.recordings && (
              <RecordingsPanel
                recording={player?.recording}
                onPlay={refresh}
                playingPath={player?.saved.source === "recording" ? player.saved.path : undefined}
              />
            )}
          </div>

          <div className="space-y-4">
            <EqPanel
              presetId={player?.eq.id ?? "flat"}
              presetName={player?.eq.name}
              enabled={eqEnabled}
              eqApplied={player?.eq.eq_applied}
              bands={player?.eq.bands}
              bandFreqs={player?.eq.band_freqs}
              onChange={refresh}
            />
            <BluetoothPanel
              bt={status?.bluetooth ?? {}}
              primaryMac={status?.primary_mac}
              onRefresh={refresh}
            />
            <NetworkPanel />
          </div>
        </main>

        <footer className="mt-8 flex items-center justify-center gap-1">
          <button
            type="button"
            onClick={() => setShowSettings(true)}
            className="flex items-center gap-2 rounded-xl px-4 py-2 text-xs text-mist transition-colors hover:bg-white/5 hover:text-white"
          >
            <Settings className="h-3.5 w-3.5" />
            Settings · Diagnostics · Shut down
          </button>
          {/* Both open off-box, which on a player that usually lives on a LAN
              with no route out will simply not load — hence a new tab, so a
              dead link never takes the UI down with it. */}
          <a
            href="https://github.com/anducampu/SAFYDO"
            target="_blank"
            rel="noreferrer noopener"
            title="Source on GitHub"
            aria-label="Source on GitHub"
            className="rounded-xl p-2 text-mist transition-colors hover:bg-white/5 hover:text-white"
          >
            <Github className="h-3.5 w-3.5" />
          </a>
          <a
            href="https://andu.ro/safydo"
            target="_blank"
            rel="noreferrer noopener"
            title="Project website"
            aria-label="Project website"
            className="rounded-xl p-2 text-mist transition-colors hover:bg-white/5 hover:text-white"
          >
            <Globe className="h-3.5 w-3.5" />
          </a>
        </footer>
      </div>

      <SettingsModal open={showSettings} onClose={() => setShowSettings(false)} />
    </div>
  );
}
