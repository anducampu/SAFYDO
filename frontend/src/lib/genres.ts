/** A station's genre field holds one or more genres separated by commas
 * ("AMBIENT, CHILL"). Every consumer goes through this, so a station shows
 * one chip per genre and sits under every genre it lists. Semicolons are
 * accepted too, since hand-edited JSON sometimes uses them. */
export function splitGenres(genre?: string | null): string[] {
  if (!genre) return [];
  const out: string[] = [];
  for (const part of genre.split(/[,;]/)) {
    const g = part.trim().toUpperCase();
    if (g && !out.includes(g)) out.push(g);
  }
  return out;
}
