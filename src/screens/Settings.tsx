import { useCallback, useEffect, useState } from 'react';
import { Ban, Captions, Gauge, History, Info, Layers, LogOut, PictureInPicture2, Play, Repeat, Server, ShieldCheck, UserCheck, UserPlus, Volume2 } from 'lucide-react';
import { getStartWith, MAX_PROFILES, PROFILE_LIMIT_MESSAGE, setStartWith, type StartWith } from '../api/startWith';
import type { AccountUser, ContentScope, HiddenTitle, Profile, ServerHistory } from '../api/types';
import { listHiddenTitles, updateProfile } from '../api/accountClient';
import { unhide } from '../api/hiddenTitles';
import {
  AUDIO_LANGUAGE_OPTIONS,
  SUBTITLE_LANGUAGE_OPTIONS,
  getPreferences,
  qualityFromTier,
  setPreference,
  tierFromQuality,
  type Preferences,
} from '../api/preferences';
import {
  SettingsGroup,
  SettingsRow,
  SettingsSelectRow,
  SettingsStatusRow,
  SettingsToggleRow,
} from '../components/SettingsGroup';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { ProfileAvatar } from '../components/ProfileAvatar';
import primitives from '../styles/primitives.module.css';
import styles from './Settings.module.css';

export interface SettingsProps {
  me: AccountUser | null;
  onAddProfile?: () => void;
  onEditProfile?: (profile: Profile) => void;
  /** Open the "Who's watching?" picker. */
  onSwitchProfile?: () => void;
  /** Called after the active profile's Playback quality was saved. */
  onProfileChanged?: () => void;
  /** Called after "Home Shows" changed: Home and search must reload. */
  onScopeChanged?: () => void;
}

const HISTORY_OPTIONS = [
  { value: 'auto', label: 'Automatic' },
  { value: 'on', label: 'On' },
  { value: 'off', label: 'Off' },
];

const SCOPE_OPTIONS = [
  { value: 'everything', label: 'Everything' },
  { value: 'my_servers', label: 'My Servers' },
];

const QUALITY_OPTIONS = [
  { value: 'original', label: 'Original' },
  { value: 'optimized', label: 'Optimized' },
];

export function Settings({ me, onAddProfile, onEditProfile, onSwitchProfile, onProfileChanged, onScopeChanged }: SettingsProps) {
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false);
  const [prefs, setPrefs] = useState<Preferences>(() => getPreferences());
  const updatePref = useCallback(<K extends keyof Preferences>(key: K, value: Preferences[K]) => {
    setPrefs(setPreference(key, value));
  }, []);

  const accountLabel = me?.email || me?.display_name || 'Curatio';
  const profiles = me?.profiles ?? [];
  // Playback quality is per profile, stored as its `bandwidth_tier`
  // (`low` = Optimized, `high`/`auto` = Original).
  const activeProfile = profiles.find((profile) => profile.id === me?.active_profile_id);
  const [quality, setQuality] = useState(() => qualityFromTier(activeProfile?.bandwidth_tier ?? me?.bandwidth_tier));
  const [qualityError, setQualityError] = useState('');
  const savedTier = activeProfile?.bandwidth_tier ?? me?.bandwidth_tier;
  useEffect(() => setQuality(qualityFromTier(savedTier)), [activeProfile?.id, savedTier]);
  const changeQuality = useCallback(async (value: string) => {
    const next = value === 'optimized' ? 'optimized' : 'original';
    if (!activeProfile) return;
    const previous = quality;
    setQuality(next);
    setQualityError('');
    try {
      await updateProfile(activeProfile.id, { bandwidth_tier: tierFromQuality(next) });
      onProfileChanged?.();
    } catch (err) {
      setQuality(previous);
      setQualityError(err instanceof Error ? err.message : 'Could not save Playback quality.');
    }
  }, [activeProfile, quality, onProfileChanged]);
  // What Home and search show for this profile (stored as `content_scope`).
  const savedScope: ContentScope = activeProfile?.content_scope === 'my_servers' ? 'my_servers' : 'everything';
  const [scope, setScope] = useState<ContentScope>(savedScope);
  const [scopeError, setScopeError] = useState('');
  useEffect(() => setScope(savedScope), [activeProfile?.id, savedScope]);
  const changeScope = useCallback(async (value: string) => {
    if (!activeProfile) return;
    const next: ContentScope = value === 'my_servers' ? 'my_servers' : 'everything';
    const previous = scope;
    setScope(next);
    setScopeError('');
    try {
      await updateProfile(activeProfile.id, { content_scope: next });
      onProfileChanged?.();
      onScopeChanged?.();
    } catch (err) {
      setScope(previous);
      setScopeError(err instanceof Error ? err.message : 'Could not change what Home shows.');
    }
  }, [activeProfile, scope, onProfileChanged, onScopeChanged]);
  // Whether the connected Plex/Jellyfin account's watch history shapes this
  // profile's recommendations ("auto": on for the server owner's profile).
  const savedHistory: ServerHistory = activeProfile?.server_history === 'on' || activeProfile?.server_history === 'off'
    ? activeProfile.server_history
    : 'auto';
  const [history, setHistory] = useState<ServerHistory>(savedHistory);
  const [historyError, setHistoryError] = useState('');
  useEffect(() => setHistory(savedHistory), [activeProfile?.id, savedHistory]);
  const changeHistory = useCallback(async (value: string) => {
    if (!activeProfile) return;
    const next: ServerHistory = value === 'on' || value === 'off' ? value : 'auto';
    const previous = history;
    setHistory(next);
    setHistoryError('');
    try {
      await updateProfile(activeProfile.id, { server_history: next });
      onProfileChanged?.();
      onScopeChanged?.();
    } catch (err) {
      setHistory(previous);
      setHistoryError(err instanceof Error ? err.message : 'Could not change watch history.');
    }
  }, [activeProfile, history, onProfileChanged, onScopeChanged]);
  const [hiddenTitles, setHiddenTitles] = useState<HiddenTitle[]>([]);
  useEffect(() => {
    if (!activeProfile) return undefined;
    const controller = new AbortController();
    listHiddenTitles(controller.signal)
      .then((data) => setHiddenTitles(data.items ?? []))
      .catch(() => undefined);
    return () => controller.abort();
  }, [activeProfile?.id]);
  const restoreHidden = useCallback(async (entry: HiddenTitle) => {
    try {
      await unhide(entry.tmdb_id, entry.media_type);
      setHiddenTitles((current) => current.filter((item) => !(item.tmdb_id === entry.tmdb_id && item.media_type === entry.media_type)));
    } catch {
      setHistoryError('Could not restore that title.');
    }
  }, []);
  const atLimit = profiles.length >= MAX_PROFILES;
  const [startWith, setStartWithState] = useState<StartWith>(() => {
    const saved = getStartWith(me);
    return saved !== 'ask' && profiles.some((profile) => profile.id === saved) ? saved : 'ask';
  });

  return (
    <div className={styles.page} data-nav-scope="settings">
      <h1 className={primitives.bigTitle}>Settings</h1>

      <SettingsGroup label="Profiles">
        {profiles.map((profile) => (
          <SettingsRow
            key={profile.id}
            icon={<ProfileAvatar name={profile.display_name} avatar={profile.avatar} size={28} />}
            tint="gray"
            label={profile.display_name || 'Profile'}
            value={profile.id === me?.active_profile_id ? 'Active' : undefined}
            onClick={onEditProfile ? () => onEditProfile(profile) : undefined}
          />
        ))}
        {activeProfile ? (
          <SettingsSelectRow
            icon={<Layers size={15} />}
            tint="orange"
            label="Home Shows"
            value={scope}
            options={SCOPE_OPTIONS}
            onChange={(value) => void changeScope(value)}
          />
        ) : null}
        {scopeError ? <SettingsRow icon={<Info size={15} />} tint="red" label="Home Shows" value={scopeError} /> : null}
        {activeProfile ? (
          <SettingsSelectRow
            icon={<History size={15} />}
            tint="purple"
            label="Use My Server's Watch History"
            value={history}
            options={HISTORY_OPTIONS}
            onChange={(value) => void changeHistory(value)}
          />
        ) : null}
        {historyError ? <SettingsRow icon={<Info size={15} />} tint="red" label="Watch History" value={historyError} /> : null}
        {profiles.length > 1 && onSwitchProfile ? (
          <SettingsRow icon={<Repeat size={15} />} tint="blue" label="Switch Profile" onClick={onSwitchProfile} />
        ) : null}
        {profiles.length > 1 ? (
          <SettingsSelectRow
            icon={<UserCheck size={15} />}
            tint="purple"
            label="Start With"
            value={String(startWith)}
            options={[{ value: 'ask', label: 'Always Ask' }, ...profiles.map((profile) => ({ value: String(profile.id), label: profile.display_name || 'Profile' }))]}
            onChange={(value) => {
              const next = value === 'ask' ? 'ask' : Number(value);
              setStartWith(me, next);
              setStartWithState(next);
            }}
          />
        ) : null}
        {atLimit ? (
          <SettingsRow icon={<UserPlus size={15} />} tint="gray" label="Add Profile" value={PROFILE_LIMIT_MESSAGE} />
        ) : (
          <SettingsRow
            icon={<UserPlus size={15} />}
            tint="green"
            label="Add Profile"
            onClick={onAddProfile}
          />
        )}
      </SettingsGroup>

      {hiddenTitles.length ? (
        <SettingsGroup label="Not for Me">
          {hiddenTitles.map((entry) => (
            <SettingsRow
              key={`${entry.media_type}:${entry.tmdb_id}`}
              icon={<Ban size={15} />}
              tint="gray"
              label={entry.title || `TMDB ${entry.tmdb_id}`}
              value="Restore"
              onClick={() => void restoreHidden(entry)}
            />
          ))}
        </SettingsGroup>
      ) : null}

      <SettingsGroup label="Playback">
        <SettingsToggleRow
          icon={<Play size={15} fill="currentColor" />}
          tint="blue"
          label="Autoplay"
          checked={prefs.autoplay}
          onChange={(value) => updatePref('autoplay', value)}
        />
        <SettingsToggleRow
          icon={<Play size={15} fill="currentColor" />}
          tint="blue"
          label="Autoplay Next"
          checked={prefs.autoplayNext}
          onChange={(value) => updatePref('autoplayNext', value)}
        />
        <SettingsToggleRow
          icon={<PictureInPicture2 size={15} />}
          tint="green"
          label="Picture in Picture"
          checked={prefs.pip}
          onChange={(value) => updatePref('pip', value)}
        />
        {activeProfile ? (
          <SettingsSelectRow
            icon={<Gauge size={15} />}
            tint="orange"
            label="Playback Quality"
            value={quality}
            options={QUALITY_OPTIONS}
            onChange={(value) => void changeQuality(value)}
          />
        ) : null}
        {qualityError ? <SettingsRow icon={<Info size={15} />} tint="red" label="Playback Quality" value={qualityError} /> : null}
        <SettingsSelectRow
          icon={<Volume2 size={15} />}
          tint="purple"
          label="Audio Language"
          value={prefs.audioLanguage}
          options={AUDIO_LANGUAGE_OPTIONS}
          onChange={(value) => updatePref('audioLanguage', value)}
        />
        <SettingsSelectRow
          icon={<Captions size={15} />}
          tint="gray"
          label="Subtitles"
          value={prefs.subtitleLanguage}
          options={SUBTITLE_LANGUAGE_OPTIONS}
          onChange={(value) => updatePref('subtitleLanguage', value)}
        />
      </SettingsGroup>

      <SettingsGroup label="Account">
        <SettingsStatusRow
          icon={<Server size={15} />}
          tint="green"
          label="Signed In"
          sub={accountLabel}
          connected
        />
        <SettingsRow icon={<ShieldCheck size={15} />} tint="orange" label="Security" value="Passkeys & 2FA" href="/account/security" />
      </SettingsGroup>

      <SettingsGroup label="About">
        <SettingsRow icon={<Info size={15} />} tint="gray" label="Version" value={__APP_VERSION__} />
        {/* TMDB's API terms require this notice wherever its data is shown. */}
        <SettingsStatusRow
          icon={<Info size={15} />}
          tint="gray"
          label="Movie and TV information and artwork: The Movie Database (TMDB)"
          sub="This product uses the TMDB API but is not endorsed or certified by TMDB."
        />
      </SettingsGroup>

      <SettingsGroup>
        <SettingsRow
          icon={<LogOut size={15} />}
          tint="red"
          label="Disconnect"
          onClick={() => setConfirmingDisconnect(true)}
        />
      </SettingsGroup>

      {confirmingDisconnect ? (
        <ConfirmDialog
          title="Disconnect?"
          message={`You'll be signed out of Curatio ${window.curatioDesktop ? 'on this computer' : 'in this browser'}.`}
          confirmLabel="Disconnect"
          onCancel={() => setConfirmingDisconnect(false)}
          onConfirm={async () => {
            try {
              await fetch('/auth/logout', { method: 'POST' });
            } catch {
              // Best effort — fall through to the landing page regardless.
            }
            window.location.href = '/';
          }}
        />
      ) : null}
    </div>
  );
}
