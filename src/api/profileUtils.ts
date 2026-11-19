import type { Profile } from './types';

/**
 * Preset avatar palette (mirrors the tile tints in styles/tokens.css). Profiles
 * store a chosen color in the existing `avatar` string as `color:#RRGGBB`; the
 * format is namespaced so we can add `emoji:` etc. later without a migration.
 */
export const AVATAR_PALETTE = [
  '#4a8fd4', // blue
  '#9b6fd4', // purple
  '#4ab46a', // green
  '#d4834a', // orange
  '#d44a4a', // red
  '#a6a6a6', // gray
] as const;

const COLOR_PREFIX = 'color:';
const HEX_RE = /^#[0-9a-fA-F]{6}$/;

/** Deterministically pick a palette color from a name (stable fallback). */
export function hashColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
}

/** Resolve the display color for a profile: explicit `color:` avatar, else hashed. */
export function parseAvatarColor(avatar?: string | null, fallbackName = 'Profile'): string {
  if (avatar && avatar.startsWith(COLOR_PREFIX)) {
    const hex = avatar.slice(COLOR_PREFIX.length).trim();
    if (HEX_RE.test(hex)) return hex;
  }
  return hashColor(fallbackName || 'Profile');
}

/** Encode a palette color back into the `avatar` string field. */
export function encodeAvatarColor(hex: string): string {
  return `${COLOR_PREFIX}${hex}`;
}

/** First letter of a profile name, for the avatar glyph. */
export function profileInitial(name?: string | null): string {
  return String(name || 'P').trim().charAt(0).toUpperCase() || 'P';
}

export function profileColor(profile: Pick<Profile, 'avatar' | 'display_name'>): string {
  return parseAvatarColor(profile.avatar, profile.display_name);
}
