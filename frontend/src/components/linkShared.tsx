import { useState } from "react";
import { clock } from "../lib/clock";
import type { WebLink } from "../types";

// Brand-adjacent hues, lifted where the real one is too dark on the glass surface.
export const PLATFORM_COLORS: Record<string, string> = {
  youtube: "#ff4e45",
  soundcloud: "#ff7a1a",
  mixcloud: "#8b7bff",
  vimeo: "#1ab7ea",
  dailymotion: "#4d94ff",
  rumble: "#85c742",
};

/** A link's length, or null when it has none (live, or unknown). */
export function duration(s?: number | null): string | null {
  return s && Number.isFinite(s) ? clock(s) : null;
}

export function Thumb({ link, small }: { link: WebLink; small?: boolean }) {
  const [broken, setBroken] = useState(false);
  const color = PLATFORM_COLORS[link.platform] ?? "#8b8798";
  if (link.thumbnail && !broken) {
    // Loaded by the viewer's browser, not the Pi (which often has no route out).
    return (
      <img
        src={link.thumbnail}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        className={`${small ? "h-7 w-11" : "h-9 w-14"} shrink-0 rounded-lg object-cover`}
      />
    );
  }
  return (
    <span
      className={`flex ${small ? "h-7 w-11" : "h-9 w-14"} shrink-0 items-center justify-center rounded-lg text-[10px] font-bold uppercase`}
      style={{ background: `${color}26`, color }}
    >
      {link.platform.slice(0, 2)}
    </span>
  );
}

