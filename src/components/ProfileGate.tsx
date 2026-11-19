import { Pencil, Plus } from 'lucide-react';
import type { Profile } from '../api/types';
import { ProfileAvatar } from './ProfileAvatar';
import primitives from '../styles/primitives.module.css';
import styles from './ProfileGate.module.css';
import { useDialogFocus } from './useDialogFocus';

export interface ProfileGateProps {
  profiles: Profile[];
  activeProfileId?: number | null;
  busy?: boolean;
  onSelect: (profile: Profile) => void;
  /** Omitted at the profile limit. */
  onAddProfile?: () => void;
  /** tvOS "Manage Profiles" under the avatars. */
  onManage?: () => void;
}

/** tvOS "Who's watching?": large rounded-square avatars in one row, a white
 * ring and lift on hover/focus. Only shown when 2+ profiles exist. */
export function ProfileGate({ profiles, activeProfileId, busy, onSelect, onAddProfile, onManage }: ProfileGateProps) {
  const dialogRef = useDialogFocus<HTMLElement>();
  return (
    <section
      ref={dialogRef}
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label="Choose a profile"
      data-nav-layer="modal"
    >
      <div className={styles.inner}>
        <h1 className={styles.title}>Who&rsquo;s watching?</h1>
        <div className={styles.grid} data-nav-row="profiles">
          {profiles.map((profile) => (
            <button
              key={profile.id}
              type="button"
              className={styles.tile}
              disabled={busy}
              data-nav-item
              data-own-focus
              data-autofocus={profile.id === activeProfileId ? '' : undefined}
              onClick={() => onSelect(profile)}
            >
              <span className={styles.avatarBox}>
                {/* One indicator only: focus (which starts on the active profile). */}
                <ProfileAvatar name={profile.display_name} avatar={profile.avatar} fill />
              </span>
              <span className={styles.name}>{profile.display_name || 'Profile'}</span>
            </button>
          ))}
          {onAddProfile ? (
            <button type="button" className={styles.tile} disabled={busy} onClick={onAddProfile} data-nav-item data-own-focus>
              <span className={`${styles.avatarBox} ${styles.addBox}`} aria-hidden="true">
                <Plus size={44} />
              </span>
              <span className={styles.name}>Add Profile</span>
            </button>
          ) : null}
        </div>
        {onManage ? (
          <div className={styles.manageRow} data-nav-row="profile-manage" data-nav-memory="off">
            <button type="button" className={primitives.ghostButton} disabled={busy} onClick={onManage} data-nav-item data-own-focus>
              <Pencil size={18} aria-hidden="true" />
              Manage Profiles
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
