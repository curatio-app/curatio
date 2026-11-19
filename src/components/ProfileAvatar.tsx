import { parseAvatarColor, profileInitial } from '../api/profileUtils';
import styles from './ProfileAvatar.module.css';

export interface ProfileAvatarProps {
  name?: string | null;
  avatar?: string | null;
  /** Edge length in px. Ignored when `fill` is set. */
  size?: number;
  /** Fill the parent box (the top bar's profile control). */
  fill?: boolean;
  selected?: boolean;
}

/** tvOS profile avatar: a rounded square (radius 16% of its size) in the
 * profile's colour with its initial. */
export function ProfileAvatar({ name, avatar, size = 40, fill = false, selected = false }: ProfileAvatarProps) {
  const color = parseAvatarColor(avatar, name ?? 'Profile');
  const initial = profileInitial(name);
  const style = fill
    ? { width: '100%', height: '100%', background: color, fontSize: '1em' }
    : { width: size, height: size, background: color, fontSize: size * 0.42, borderRadius: size * 0.16 };
  return (
    <span
      className={`${styles.avatar} ${fill ? styles.fill : ''} ${selected ? styles.selected : ''}`}
      style={style}
      aria-hidden="true"
    >
      {initial}
    </span>
  );
}
