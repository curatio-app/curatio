/**
 * Profile rules shared with tvOS/iOS:
 *   - "Start With" (SettingsView): open a chosen profile at launch, or always ask.
 *     Scoped to the account so a shared computer never opens someone else's profile.
 *   - the 10-profile cap the server enforces (ProfileManagementView).
 */
import type { AccountUser, Profile } from './types';

export const MAX_PROFILES = 10;
export const PROFILE_LIMIT_MESSAGE = `You've reached the limit of ${MAX_PROFILES} profiles.`;

export type StartWith = 'ask' | number;

function storageKey(me: AccountUser | null | undefined): string {
  return `curatio.startWith.${me?.id ?? 'account'}`;
}

export function getStartWith(me: AccountUser | null | undefined): StartWith {
  try {
    const raw = localStorage.getItem(storageKey(me));
    const id = raw == null ? NaN : Number(raw);
    return Number.isInteger(id) && id > 0 ? id : 'ask';
  } catch {
    return 'ask';
  }
}

export function setStartWith(me: AccountUser | null | undefined, value: StartWith) {
  try {
    if (value === 'ask') localStorage.removeItem(storageKey(me));
    else localStorage.setItem(storageKey(me), String(value));
  } catch {
    // A convenience only: without storage the picker just keeps asking.
  }
}

/** The profile to open at launch, or null to ask (or when the saved one was deleted). */
export function startWithProfile(me: AccountUser | null | undefined, profiles: Profile[]): Profile | null {
  const choice = getStartWith(me);
  return choice === 'ask' ? null : profiles.find((profile) => profile.id === choice) ?? null;
}
