import { Volume2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useRangeCommit } from "../hooks/useRangeCommit";

interface Props {
  volume: number;
  hwVolume?: number | null;
  followHardware?: boolean;
  speakerIgnoresVolume?: boolean;
  /** Connected speaker has no remote volume: only the player's level exists. */
  speakerNoRemoteVolume?: boolean;
  /** The saved level: in sync mode mpv.volume is a fixed 100 and says nothing. */
  volumeConfig?: number;
  onChange: () => void;
}

export function VolumeSlider({
  volume,
  hwVolume,
  followHardware = false,
  speakerIgnoresVolume = false,
  speakerNoRemoteVolume = false,
  volumeConfig,
  onChange,
}: Props) {
  const source = !followHardware || speakerIgnoresVolume || speakerNoRemoteVolume
    ? volume
    : hwVolume ?? volumeConfig ?? volume;
  const [local, setLocal] = useState(source);

  useEffect(() => {
    setLocal(source);
  }, [source]);

  const commitSeq = useRef(0);

  const commit = async (v: number) => {
    const seq = ++commitSeq.current;
    // The player's level, not `local`: onChange has already moved that to v,
    // so rolling back to it after a failed request restored nothing.
    const previous = source;
    setLocal(v);
    try {
      await api.volume(v);
      if (seq === commitSeq.current) onChange();
    } catch {
      if (seq === commitSeq.current) setLocal(previous);
    }
  };

  const rangeRef = useRangeCommit((v) => void commit(v));

  const toggleFollow = async () => {
    try {
      await api.volumeFollow(!followHardware);
    } catch {
      /* the status re-read below puts the checkbox back where it really is */
    }
    onChange();
  };

  return (
    <div className="glass rounded-xl px-4 py-3">
      <div className="flex items-center gap-3">
        <Volume2 className="h-5 w-5 shrink-0 text-mist" />
        <input
          ref={rangeRef}
          type="range"
          min={0}
          max={100}
          value={local}
          onChange={(e) => setLocal(Number(e.target.value))}
          className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-white/10 accent-[var(--accent)]"
          aria-label={
            followHardware
              ? "Volume (synced with speaker)"
              : "Volume"
          }
        />
        <span className="w-8 text-right text-sm tabular-nums text-mist">
          {local}
        </span>
      </div>
      <label className="mt-2 flex cursor-pointer items-center justify-end gap-2 text-xs text-mist">
        <span>Sync with speaker</span>
        <input
          type="checkbox"
          checked={followHardware}
          onChange={() => void toggleFollow()}
          className="accent-[var(--accent)]"
        />
      </label>
      {followHardware && (
        <p className="mt-1 text-right text-[10px] text-mist/60">
          {speakerNoRemoteVolume
            ? "This speaker has no remote volume, so the slider only sets the player's level"
            : speakerIgnoresVolume
              ? "This speaker ignores volume commands, so the slider only sets the player's level"
              : "Slider and speaker buttons control the same volume"}
        </p>
      )}
    </div>
  );
}