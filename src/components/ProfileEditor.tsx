import { useEffect, useMemo, useState, useRef } from 'react';
import { Check, X } from 'lucide-react';
import { createProfile, deleteProfile, rotateProfileKey, updateProfile } from '../api/accountClient';
import type { Profile } from '../api/types';
import { AVATAR_PALETTE, encodeAvatarColor, parseAvatarColor } from '../api/profileUtils';
import { ProfileAvatar } from './ProfileAvatar';
import primitives from '../styles/primitives.module.css';
import styles from './ProfileEditor.module.css';
import { useDialogFocus } from './useDialogFocus';

export interface ProfileEditorProps {
  /** Profile to edit, or null to create a new one. */
  profile: Profile | null;
  /** True when this is the account's only profile (delete disabled). */
  canDelete: boolean;
  onClose: () => void;
  /** Called after a successful create/edit so the parent can refresh state. */
  onSaved: () => void;
  /** Called after a successful delete. */
  onDeleted: () => void;
}

export function ProfileEditor({ profile, canDelete, onClose, onSaved, onDeleted }: ProfileEditorProps) {
  const dialogRef = useDialogFocus<HTMLElement>();
  const isEdit = profile != null;
  const [name, setName] = useState(profile?.display_name ?? '');
  const [color, setColor] = useState(() => parseAvatarColor(profile?.avatar, profile?.display_name));
  // Playback quality, stored as the profile's bandwidth tier (low = Optimized).
  const [bandwidth, setBandwidth] = useState<'low' | 'high'>(
    String(profile?.bandwidth_tier ?? '').toLowerCase() === 'low' ? 'low' : 'high',
  );
  const [busy, setBusy] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingRotate, setConfirmingRotate] = useState(false);
  const [rotated, setRotated] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const savingRef = useRef(false);
  const trimmed = name.trim();
  const previewName = useMemo(() => trimmed || 'Profile', [trimmed]);

  async function save() {
    if (savingRef.current) return; // a second Enter or click must not create a second profile
    if (!trimmed) {
      setError('Please enter a name.');
      return;
    }
    savingRef.current = true;
    setBusy(true);
    setError('');
    try {
      const payload = {
        display_name: trimmed,
        avatar: encodeAvatarColor(color),
        bandwidth_tier: bandwidth,
      };
      if (isEdit && profile) {
        await updateProfile(profile.id, payload);
      } else {
        await createProfile(payload);
      }
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the profile.');
      savingRef.current = false;
      setBusy(false);
    }
  }

  async function remove() {
    if (!profile) return;
    setBusy(true);
    setError('');
    try {
      await deleteProfile(profile.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete the profile.');
      setBusy(false);
      setConfirmingDelete(false);
    }
  }

  async function rotateKey() {
    if (!profile) return;
    setBusy(true);
    setError('');
    try {
      await rotateProfileKey(profile.id);
      setRotated(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign out the apps.');
    } finally {
      setBusy(false);
      setConfirmingRotate(false);
    }
  }

  return (
    <section ref={dialogRef} className={styles.overlay} role="dialog" aria-modal="true" aria-label={isEdit ? 'Edit profile' : 'Create profile'} data-nav-layer="modal" data-nav-mode="native">
      <div className={styles.scroll}>
        <header className={styles.header}>
          <button type="button" className={styles.iconBtn} onClick={onClose} disabled={busy} aria-label="Close">
            <X size={20} />
          </button>
          <h1 className={styles.title}>{isEdit ? 'Edit Profile' : 'New Profile'}</h1>
        </header>

        <div className={styles.body}>
          <div className={styles.preview}>
            <ProfileAvatar name={previewName} avatar={encodeAvatarColor(color)} size={96} />
          </div>

          <label className={styles.label} htmlFor="profile-name">Name</label>
          <div className={styles.field}>
            <input
              id="profile-name"
              type="text"
              value={name}
              maxLength={60}
              placeholder="Profile name"
              data-autofocus
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.repeat) void save();
              }}
            />
          </div>

          <span className={styles.label}>Color</span>
          <div className={styles.swatches} role="radiogroup" aria-label="Profile color">
            {AVATAR_PALETTE.map((hex) => (
              <button
                key={hex}
                type="button"
                role="radio"
                aria-checked={hex === color}
                aria-label={`Color ${hex}`}
                className={`${styles.swatch} ${hex === color ? styles.swatchActive : ''}`}
                style={{ background: hex }}
                onClick={() => setColor(hex)}
              >
                {hex === color ? <Check size={16} /> : null}
              </button>
            ))}
          </div>

          <label className={styles.label} htmlFor="profile-bandwidth">Playback Quality</label>
          <div className={styles.field}>
            <select
              id="profile-bandwidth"
              className={styles.select}
              value={bandwidth}
              onChange={(e) => setBandwidth(e.target.value === 'low' ? 'low' : 'high')}
            >
              <option value="high">Original</option>
              <option value="low">Optimized</option>
            </select>
          </div>

          {error ? <p className={styles.error}>{error}</p> : null}

          <div className={styles.buttons}>
            <button type="button" className={primitives.primaryButton} onClick={() => void save()} disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className={primitives.ghostButton} onClick={onClose} disabled={busy}>
              Cancel
            </button>
          </div>

          {isEdit ? (
            <div className={styles.danger}>
              {rotated ? (
                <p className={styles.dangerText}>Apps on this profile are signed out. Pair them again to keep watching.</p>
              ) : confirmingRotate ? (
                <>
                  <p className={styles.dangerText}>
                    Sign out every TV and phone app using <strong>{profile?.display_name || 'this profile'}</strong>?
                    Watch history stays; each app has to pair again.
                  </p>
                  <div className={styles.buttons}>
                    <button type="button" className={styles.deleteBtn} onClick={() => void rotateKey()} disabled={busy}>
                      Sign Out Apps
                    </button>
                    <button type="button" className={primitives.ghostButton} onClick={() => setConfirmingRotate(false)} disabled={busy}>
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                <button type="button" className={styles.deleteLink} onClick={() => setConfirmingRotate(true)} disabled={busy}>
                  Sign Out Apps on This Profile
                </button>
              )}
            </div>
          ) : null}

          {isEdit ? (
            <div className={styles.danger}>
              {confirmingDelete ? (
                <>
                  <p className={styles.dangerText}>
                    Delete <strong>{profile?.display_name || 'this profile'}</strong> and all of its
                    watch history? This can&rsquo;t be undone.
                  </p>
                  <div className={styles.buttons}>
                    <button type="button" className={styles.deleteBtn} onClick={() => void remove()} disabled={busy}>
                      Delete Profile
                    </button>
                    <button
                      type="button"
                      className={primitives.ghostButton}
                      onClick={() => setConfirmingDelete(false)}
                      disabled={busy}
                    >
                      Keep
                    </button>
                  </div>
                </>
              ) : (
                <button
                  type="button"
                  className={styles.deleteLink}
                  onClick={() => setConfirmingDelete(true)}
                  disabled={busy || !canDelete}
                  title={canDelete ? undefined : 'You must keep at least one profile'}
                >
                  Delete Profile
                </button>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
