import { useEffect, useRef } from 'react';
import { Check, Plus, Settings, Settings2, Users } from 'lucide-react';
import type { Profile } from '../api/types';
import { ProfileAvatar } from './ProfileAvatar';
import styles from './ProfileSwitcher.module.css';

export interface ProfileSwitcherProps {
  profiles: Profile[];
  activeProfileId?: number | null;
  busy?: boolean;
  onSelect: (profile: Profile) => void;
  onManage: () => void;
  onAddProfile: () => void;
  onWhosWatching: () => void;
  onSettings?: () => void;
  onClose: () => void;
}

/**
 * The top bar's avatar menu: switch profile, "Who's watching?", add/manage
 * profiles and Settings. ↑/↓ move, Escape closes and returns focus to the
 * avatar, Tab leaves.
 */
export function ProfileSwitcher({
  profiles,
  activeProfileId,
  busy,
  onSelect,
  onManage,
  onAddProfile,
  onWhosWatching,
  onSettings,
  onClose,
}: ProfileSwitcherProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const first =
      menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]') ??
      menuRef.current?.querySelector<HTMLElement>('button:not(:disabled)');
    first?.focus();
    return () => {
      if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
    };
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled)') ?? []);
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      (event.key === 'Home' ? items[0] : items[items.length - 1])?.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    } else if (event.key === 'Tab') {
      onClose();
    }
  };

  return (
    <>
      <div className={styles.backdrop} onClick={onClose} aria-hidden="true" />
      <div
        className={styles.menu}
        role="menu"
        aria-label="Profiles and settings"
        ref={menuRef}
        onKeyDown={onKeyDown}
        data-nav-layer="modal"
        data-nav-mode="native"
      >
        <p className={styles.heading}>Profiles</p>
        <div className={styles.list}>
          {profiles.map((profile) => {
            const active = profile.id === activeProfileId;
            return (
              <button
                key={profile.id}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                className={styles.row}
                disabled={busy}
                onClick={() => (active ? onClose() : onSelect(profile))}
              >
                <ProfileAvatar name={profile.display_name} avatar={profile.avatar} size={30} />
                <span className={styles.name}>{profile.display_name || 'Profile'}</span>
                {active ? <Check size={16} className={styles.check} /> : null}
              </button>
            );
          })}
        </div>
        <div className={styles.actions}>
          {profiles.length > 1 ? (
            <button type="button" role="menuitem" className={styles.action} onClick={onWhosWatching}>
              <Users size={16} />
              <span>Who&rsquo;s watching?</span>
            </button>
          ) : null}
          <button type="button" role="menuitem" className={styles.action} onClick={onAddProfile}>
            <Plus size={16} />
            <span>Add Profile</span>
          </button>
          <button type="button" role="menuitem" className={styles.action} onClick={onManage}>
            <Settings2 size={16} />
            <span>Manage Profiles</span>
          </button>
          {onSettings ? (
            <button type="button" role="menuitem" className={styles.action} onClick={onSettings}>
              <Settings size={16} />
              <span>Settings</span>
            </button>
          ) : null}
        </div>
      </div>
    </>
  );
}
