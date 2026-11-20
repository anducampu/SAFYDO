import { splitGenres } from "./genres";

// Colours chosen to suggest the genre's mood, bright enough to read as text
// on the dark surface and as the whole UI's accent when that genre plays.
const GENRE_MAP: Record<string, string> = {
  // ── the station list's own genres ──
  HOUSE: "#ff6b35",
  DEEP: "#3a86ff", // deep house: late-night blue
  DANCE: "#f72585",
  DISCO: "#ff70a6", // mirror-ball pink
  CHILL: "#4ecdc4",
  AMBIENT: "#7b8cde", // misty indigo
  JAZZ: "#c9a227",
  ROCK: "#e63946",
  CLASSIC: "#a8dadc",
  AC: "#9b5de5",
  CHR: "#00bbf9",
  TOP40: "#fee440",
  HITS: "#ffd166", // chart gold, next to TOP40
  TALK: "#90be6d",
  REGIO: "#7d9cc0", // lifted from #577590: too dim as text on the dark surface
  PSY: "#b5179e",
  DNB: "#a855f7", // lifted from #7209b7: too dark to read
  REGGAE: "#2a9d8f",
  ECLECTIC: "#e76f51",
  VARIETY: "#48cae4",
  VA: "#6c757d",
  "EASY LISTENING": "#cdb4db",

  // ── electronic ──
  ELECTRONIC: "#00f5d4",
  TECHNO: "#8338ec",
  TRANCE: "#5e60ce",
  LOUNGE: "#e9c46a",
  DOWNTEMPO: "#84a59d",
  LOFI: "#b8a1d9",
  DUBSTEP: "#06d6a0",
  EDM: "#ff4d6d",
  GARAGE: "#43aa8b",

  // ── pop, urban, roots ──
  POP: "#ff5d8f",
  INDIE: "#f4a261",
  ALTERNATIVE: "#e5989b",
  PUNK: "#ff006e",
  METAL: "#c0c7d1",
  BLUES: "#4361ee",
  SOUL: "#d4a373",
  FUNK: "#fb8500",
  RNB: "#c77dff",
  "HIP HOP": "#ffbe0b",
  RAP: "#ffbe0b",
  COUNTRY: "#bc6c25",
  FOLK: "#a7c957",
  LATIN: "#ef476f",
  WORLD: "#2ec4b6",
  GOSPEL: "#f9c74f",

  // ── eras ──
  OLDIES: "#d4a5a5",
  RETRO: "#d4a5a5",
  "80S": "#f15bb5",
  "90S": "#00b4d8",
  "00S": "#f8961e",

  // ── serious listening ──
  OPERA: "#b56576",
  SOUNDTRACK: "#a5a1c2",
  NEWS: "#90a4ae",
  SPORTS: "#52b788",
  KIDS: "#80ed99",

  // ── labels the player itself shows in Now Playing ──
  PLAYLIST: "#a29bfe",
  RECORDING: "#ff6b6b",
  FM: "#48bfe3",
  "WEB LINK": "#a29bfe",
  YOUTUBE: "#ff4e45",
  SOUNDCLOUD: "#ff7a1a",
  MIXCLOUD: "#8b7bff",
  VIMEO: "#1ab7ea",
  DAILYMOTION: "#4d94ff",
  RUMBLE: "#85c742",
};

// Other spellings of the same genre.
const ALIASES: Record<string, string> = {
  CLASSICAL: "CLASSIC",
  "TOP 40": "TOP40",
  HIPHOP: "HIP HOP",
  "R&B": "RNB",
  "R N B": "RNB",
  "LO FI": "LOFI",
  "DRUM AND BASS": "DNB",
  "DRUM & BASS": "DNB",
  "DRUM N BASS": "DNB",
  "DEEP HOUSE": "DEEP",
  PSYTRANCE: "PSY",
  "PSY TRANCE": "PSY",
  ELECTRO: "ELECTRONIC",
  CHILLOUT: "CHILL",
  "CHILL OUT": "CHILL",
  "1980S": "80S",
  "1990S": "90S",
  "2000S": "00S",
};

function lookup(genre: string): string | undefined {
  const key = genre.replace(/-/g, " ").replace(/\s+/g, " ").trim();
  const canon = ALIASES[key] ?? key;
  return GENRE_MAP[canon] ?? GENRE_MAP[canon.replace(/\s/g, "")];
}

/** A genre nobody put in the map still gets its own colour, the same one
 * every time: hue from a hash of the name, fixed saturation and lightness
 * so it reads like the hand-picked ones. */
function hashedColour(genre: string): string {
  let h = 0;
  for (const ch of genre) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return hslToHex(h % 360, 70, 64);
}

function hslToHex(h: number, s: number, l: number): string {
  const a = (s / 100) * Math.min(l / 100, 1 - l / 100);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const c = l / 100 - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(c * 255)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

const DEFAULT = "#6c5ce7";

export function genreAccent(genre?: string): string {
  // "AMBIENT, CHILL": the first genre's colour. Every genre has one now —
  // mapped, aliased, or derived from its name.
  const first = splitGenres(genre)[0];
  if (!first) return DEFAULT;
  return lookup(first) ?? hashedColour(first);
}

const HEX = /^#[0-9a-f]{6}$/i;

/** A station's colour: the one picked in the editor, else its first genre's. */
export function stationAccent(s?: { color?: string; genre?: string } | null): string {
  return s?.color && HEX.test(s.color) ? s.color : genreAccent(s?.genre);
}

export function applyAccent(station?: { color?: string; genre?: string } | null) {
  const accent = stationAccent(station);
  document.documentElement.style.setProperty("--accent", accent);
  document.documentElement.style.setProperty(
    "--accent-glow",
    `${accent}55`,
  );
}
