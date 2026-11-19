// Lightweight, persisted client preferences for the web player.
//
// These are stored in localStorage (no server round-trip needed) and read by
// the Settings screen and the in-browser player. Audio/subtitle values are
// either the sentinels below or a short language token matched loosely against
// the track labels the browser/HLS exposes.

const STORAGE_KEY = 'curatio-preferences';

export type Preferences = {
  /** Play starts the best source directly; off opens the source picker first (native `autoplayEnabled`). */
  autoplay: boolean;
  /** Count down and play the next episode (iPad "Autoplay Next"). */
  autoplayNext: boolean;
  /** Offer Picture in Picture in the player (iPad `pictureInPictureEnabled`). */
  pip: boolean;
  audioLanguage: string;
  subtitleLanguage: string;
};

type PreferenceOption = { value: string; label: string };

// The iPad Settings lists (iOSSettingsView.audioLanguageOptions /
// subtitleLanguageOptions), with "system" standing in for the nil code.
const LANGUAGES: PreferenceOption[] = [
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Spanish' },
  { value: 'fr', label: 'French' },
  { value: 'de', label: 'German' },
  { value: 'it', label: 'Italian' },
  { value: 'pt', label: 'Portuguese' },
  { value: 'ja', label: 'Japanese' },
  { value: 'ko', label: 'Korean' },
  { value: 'zh', label: 'Chinese' },
  { value: 'ru', label: 'Russian' },
  { value: 'ar', label: 'Arabic' },
  { value: 'hi', label: 'Hindi' },
  { value: 'pl', label: 'Polish' },
  { value: 'nl', label: 'Dutch' },
  { value: 'sv', label: 'Swedish' },
  { value: 'da', label: 'Danish' },
  { value: 'no', label: 'Norwegian' },
  { value: 'fi', label: 'Finnish' },
  { value: 'tr', label: 'Turkish' },
  { value: 'uk', label: 'Ukrainian' },
  { value: 'cs', label: 'Czech' },
  { value: 'hu', label: 'Hungarian' },
  { value: 'ro', label: 'Romanian' },
];

export const AUDIO_LANGUAGE_OPTIONS: PreferenceOption[] = [
  { value: 'system', label: 'System Default' },
  ...LANGUAGES,
];

export const SUBTITLE_LANGUAGE_OPTIONS: PreferenceOption[] = [
  { value: 'system', label: 'System Default' },
  { value: 'off', label: 'Off' },
  ...LANGUAGES.slice(0, 14),
];

const DEFAULT_PREFERENCES: Preferences = {
  autoplay: true,
  autoplayNext: true,
  pip: true,
  audioLanguage: 'system',
  subtitleLanguage: 'off',
};

export function getPreferences(): Preferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_PREFERENCES };
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    return {
      autoplay: typeof parsed.autoplay === 'boolean' ? parsed.autoplay : DEFAULT_PREFERENCES.autoplay,
      autoplayNext:
        typeof parsed.autoplayNext === 'boolean' ? parsed.autoplayNext : DEFAULT_PREFERENCES.autoplayNext,
      pip: typeof parsed.pip === 'boolean' ? parsed.pip : DEFAULT_PREFERENCES.pip,
      audioLanguage:
        typeof parsed.audioLanguage === 'string' ? parsed.audioLanguage : DEFAULT_PREFERENCES.audioLanguage,
      subtitleLanguage:
        typeof parsed.subtitleLanguage === 'string'
          ? parsed.subtitleLanguage
          : DEFAULT_PREFERENCES.subtitleLanguage,
    };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

/**
 * The quality Play asks for first: `default`, i.e. the active profile's saved
 * Playback quality (Settings), which the server applies when the request
 * sends `quality: null`.
 */
export function defaultSourceTier(): 'default' {
  return 'default';
}

/** Profile `bandwidth_tier` → Playback quality: `low` is Optimized; `high`/`auto` are Original. */
export function qualityFromTier(tier: unknown): 'original' | 'optimized' {
  return String(tier ?? '').toLowerCase() === 'low' ? 'optimized' : 'original';
}

/** Playback quality → the profile `bandwidth_tier` it is stored as. */
export function tierFromQuality(quality: 'original' | 'optimized'): 'low' | 'high' {
  return quality === 'optimized' ? 'low' : 'high';
}

export function setPreference<K extends keyof Preferences>(key: K, value: Preferences[K]): Preferences {
  const next = { ...getPreferences(), [key]: value };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Preferences are a convenience only; ignore storage failures.
  }
  return next;
}

// Loose aliases so a stored token like "en" matches "English", "eng", etc.
const LANGUAGE_ALIASES: Record<string, string[]> = {
  en: ['english', 'eng', 'en'],
  es: ['spanish', 'espanol', 'español', 'spa', 'es'],
  fr: ['french', 'francais', 'français', 'fre', 'fra', 'fr'],
  de: ['german', 'deutsch', 'ger', 'deu', 'de'],
  ja: ['japanese', 'nihongo', '日本語', 'jpn', 'ja', 'jp'],
  it: ['italian', 'italiano', 'ita'],
  pt: ['portuguese', 'português', 'portugues', 'por'],
  ko: ['korean', '한국어', 'kor'],
  zh: ['chinese', 'mandarin', 'cantonese', '中文', 'chi', 'zho'],
  ru: ['russian', 'русский', 'rus'],
  ar: ['arabic', 'العربية', 'ara'],
  hi: ['hindi', 'हिन्दी', 'hin'],
  pl: ['polish', 'polski', 'pol'],
  nl: ['dutch', 'nederlands', 'nld', 'dut'],
  sv: ['swedish', 'svenska', 'swe'],
  da: ['danish', 'dansk', 'dan'],
  no: ['norwegian', 'norsk', 'nor', 'nob'],
  fi: ['finnish', 'suomi', 'fin'],
  tr: ['turkish', 'türkçe', 'turkce', 'tur'],
  uk: ['ukrainian', 'українська', 'ukr'],
  cs: ['czech', 'čeština', 'cestina', 'ces', 'cze'],
  hu: ['hungarian', 'magyar', 'hun'],
  ro: ['romanian', 'română', 'romana', 'ron', 'rum'],
};

/**
 * Return true when a track's free-text label/detail plausibly matches the
 * preferred language token. Matching is intentionally permissive — a miss just
 * means we leave the browser's default selection alone.
 */
export function trackMatchesLanguage(haystack: string, language: string): boolean {
  if (!language || language === 'system' || language === 'off') return false;
  const text = haystack.toLowerCase();
  const needles = LANGUAGE_ALIASES[language] ?? [language.toLowerCase()];
  return needles.some((needle) => needle.length > 2 && text.includes(needle));
}
