import { useState } from "react";
import { logoUrl } from "../api/client";
import { stationAccent } from "../lib/genreColors";
import { splitGenres } from "../lib/genres";

interface Props {
  logo?: string;
  genre: string;
  color?: string;
  /** Tailwind size + radius classes for the tile. */
  className?: string;
}

/**
 * A station's logo on a light tile (most logos are drawn for white), or the
 * genre monogram when there is none — or when the stored file fails to load.
 */
export function StationLogo({ logo, genre, color, className = "h-9 w-9 rounded-lg" }: Props) {
  const [failed, setFailed] = useState<string | null>(null);
  const accent = stationAccent({ color, genre });
  if (logo && failed !== logo) {
    return (
      <span className={`flex shrink-0 items-center justify-center overflow-hidden bg-white ${className}`}>
        <img
          src={logoUrl(logo)}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(logo)}
          className="h-full w-full object-contain"
        />
      </span>
    );
  }
  return (
    <span
      className={`flex shrink-0 items-center justify-center text-xs font-bold uppercase ${className}`}
      style={{ background: `${accent}22`, color: accent }}
    >
      {(splitGenres(genre)[0] ?? "").slice(0, 2) || "—"}
    </span>
  );
}
