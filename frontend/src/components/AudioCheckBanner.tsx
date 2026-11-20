import { AlertTriangle } from "lucide-react";
import type { AudioCheck } from "../types";

interface Props {
  check?: AudioCheck | null;
}

const LABELS: Record<string, string> = {
  primary_mac_empty: "No Bluetooth speaker configured",
  bluetooth_not_connected: "Bluetooth speaker disconnected",
  mpv_service_inactive: "Player service not running",
  mpv_socket_missing: "Player not ready — connect a Bluetooth speaker",
  audio_device_mismatch: "Audio output mismatch — retry play",
  playback_not_active: "Playback not active",
};

// audio_check already carries "mpv_socket_missing" (system.py _light_audio_check
// derives it from the same mpv.available() call), so no separate readiness flag
// is needed — a second one only produced an unreachable branch.
export function AudioCheckBanner({ check }: Props) {
  if (!check || check.ok) return null;
  const items = (check.failures ?? []).map((f) => LABELS[f] ?? f);
  if (!items.length) return null;

  return (
    <div className="flex gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <ul className="space-y-0.5 text-xs">
        {items.map((msg) => (
          <li key={msg}>{msg}</li>
        ))}
      </ul>
    </div>
  );
}