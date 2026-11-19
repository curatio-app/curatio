import type { AccountUser, Profile } from '../api/types';

/** The profile the account is currently viewing as (falls back to the first). */
export function activeProfileOf(me: AccountUser | null): Profile | null {
  if (!me?.profiles?.length) return null;
  return me.profiles.find((profile) => profile.id === me.active_profile_id) ?? me.profiles[0];
}
