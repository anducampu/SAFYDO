/** "m:ss", or "h:mm:ss" from an hour up; "—" when there is no number. */
export function clock(s?: number | null): string {
  if (s == null || !Number.isFinite(s)) return "—";
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = (t % 60).toString().padStart(2, "0");
  return h ? `${h}:${m.toString().padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}
