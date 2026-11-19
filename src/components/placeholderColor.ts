const PLACEHOLDERS = ['#0f1724', '#0a1f1f', '#210f17', '#1a1424', '#1f1a0d', '#121a12', '#1c120d', '#141419'];

/** A stable fallback tile colour for a title whose artwork is missing (FNV-1a). */
export function placeholderColor(seed: string) {
  let hash = 0x811c9dc5;
  for (let i = 0; i < seed.length; i += 1) {
    hash ^= seed.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return PLACEHOLDERS[Math.abs(hash) % PLACEHOLDERS.length];
}
