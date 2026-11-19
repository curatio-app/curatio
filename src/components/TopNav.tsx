import { forwardRef, useImperativeHandle, useRef } from 'react';
import { Search, X } from 'lucide-react';
import type { AccountUser } from '../api/types';
import { BROWSE_TABS, useNav } from '../app/navigation';
import { ProfileAvatar } from './ProfileAvatar';
import { activeProfileOf } from './profileOf';
import styles from './TopNav.module.css';

export interface TopNavHandle {
  focusSearch: () => void;
  focusActiveTab: () => void;
  contains: (element: Element | null) => boolean;
}

export interface TopNavProps {
  me: AccountUser | null;
  menuOpen: boolean;
  onAvatar: () => void;
}

/**
 * The pinned desktop top bar: wordmark, the tvOS glass capsule of browse tabs,
 * the search field and the profile avatar (whose menu holds profile switching
 * and Settings). It never hides; content scrolls under a black fade.
 */
export const TopNav = forwardRef<TopNavHandle, TopNavProps>(function TopNav({ me, menuOpen, onAvatar }, ref) {
  const { activeTab, setTab, searchQuery, setSearchQuery } = useNav();
  const rootRef = useRef<HTMLElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const profile = activeProfileOf(me);
  const profileName = profile?.display_name || me?.display_name || 'Profile';

  useImperativeHandle(ref, () => ({
    focusSearch: () => {
      if (activeTab !== 'search') setTab('search');
      searchRef.current?.focus();
      searchRef.current?.select();
    },
    focusActiveTab: () => {
      rootRef.current?.querySelector<HTMLElement>('[data-nav-tab][aria-current="page"]')?.focus();
    },
    contains: (element) => Boolean(element && rootRef.current?.contains(element)),
  }));

  return (
    <header className={styles.bar} ref={rootRef} data-top-nav>
      <div className={styles.row} data-nav-row="nav" data-nav-memory="off">
        <span className={styles.wordmark} aria-hidden="true">curatio</span>
        <nav className={styles.capsule} aria-label="Primary">
          {BROWSE_TABS.map(({ key, label }) => {
            const selected = activeTab === key;
            return (
              <button
                key={key}
                type="button"
                className={`${styles.tab} ${selected ? styles.selected : ''}`}
                aria-current={selected ? 'page' : undefined}
                data-nav-item
                data-nav-tab
                data-own-focus
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            );
          })}
        </nav>
        <div className={styles.trailing}>
          <label className={`${styles.search} ${activeTab === 'search' ? styles.searchActive : ''}`}>
            <Search size={17} aria-hidden="true" className={styles.searchIcon} />
            <input
              ref={searchRef}
              type="search"
              aria-label="Search"
              placeholder="Search"
              value={searchQuery}
              data-nav-item
              data-nav-search
              data-own-focus
              spellCheck={false}
              autoComplete="off"
              // Arrowing across the bar must not open Search: only a click or typing does.
              onClick={() => {
                if (activeTab !== 'search') setTab('search');
              }}
              onChange={(event) => {
                setSearchQuery(event.target.value);
                if (activeTab !== 'search') setTab('search');
              }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  if (searchQuery) setSearchQuery('');
                  else searchRef.current?.blur();
                }
              }}
            />
            {searchQuery ? (
              <button
                type="button"
                className={styles.clear}
                aria-label="Clear search"
                tabIndex={-1}
                onClick={() => {
                  setSearchQuery('');
                  searchRef.current?.focus();
                }}
              >
                <X size={15} />
              </button>
            ) : null}
          </label>
          <button
            type="button"
            className={styles.avatar}
            onClick={onAvatar}
            aria-label={`Profile: ${profileName}. Profiles and settings`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            data-nav-item
            data-own-focus
          >
            <ProfileAvatar name={profileName} avatar={profile?.avatar} size={0} fill />
          </button>
        </div>
      </div>
    </header>
  );
});
