import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useQuery } from '@tanstack/react-query';
import { NavLink, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useNavigationType } from 'react-router-dom';
import {
  Activity, Blocks, CalendarClock, Command, Database, Fingerprint, Gauge,
  KeyRound, LibraryBig, LogOut, Menu, Plug, Radio, Search, Settings, Tags, Ticket, Users,
  Wrench, X,
} from 'lucide-react';
import { adminApi } from './api';
import { LoadingPanel, PageHeader } from './components';
import type { IntegrationsResponse } from './pages/IntegrationsPage';

const OverviewPage = lazy(() => import('./pages/OverviewPage'));
const UsersPage = lazy(() => import('./pages/AccessPages').then((m) => ({ default: m.UsersPage })));
const InvitesPage = lazy(() => import('./pages/AccessPages').then((m) => ({ default: m.InvitesPage })));
const SessionsPage = lazy(() => import('./pages/AccessPages').then((m) => ({ default: m.SessionsPage })));
const StreamsPage = lazy(() => import('./pages/PlaybackPages').then((m) => ({ default: m.StreamsPage })));
const IntegrationsPage = lazy(() => import('./pages/IntegrationsPage'));
const AddonPage = lazy(() => import('./pages/AddonPage'));
const CatalogsPage = lazy(() => import('./pages/DiscoveryPages').then((m) => ({ default: m.CatalogsPage })));
const TaggingPage = lazy(() => import('./pages/DiscoveryPages').then((m) => ({ default: m.TaggingPage })));
const TagQualityPage = lazy(() => import('./pages/TagQualityPage'));
const JobsPage = lazy(() => import('./pages/OperationsPages').then((m) => ({ default: m.JobsPage })));
const SchedulePage = lazy(() => import('./pages/OperationsPages').then((m) => ({ default: m.SchedulePage })));
const FingerprintsPage = lazy(() => import('./pages/OperationsPages').then((m) => ({ default: m.FingerprintsPage })));
const SettingsPage = lazy(() => import('./pages/SystemPages').then((m) => ({ default: m.SettingsPage })));
const DiagnosticsPage = lazy(() => import('./pages/SystemPages').then((m) => ({ default: m.DiagnosticsPage })));

type NavItem = { to: string; label: string; icon: typeof Gauge; description: string; end?: boolean };
type NavGroup = { label: string; links: NavItem[] };

const groups: NavGroup[] = [
  { label: 'Home', links: [{ to: '/overview', label: 'Overview', icon: Gauge, description: 'Workspace status and setup' }] },
  { label: 'Access', links: [{ to: '/users', label: 'Users', icon: Users, description: 'Viewer accounts and permissions' }, { to: '/invites', label: 'Invites', icon: Ticket, description: 'Invitation links' }, { to: '/sessions', label: 'Sessions', icon: KeyRound, description: 'Signed-in devices' }] },
  { label: 'Playback', links: [{ to: '/streams', label: 'Live Activity', icon: Radio, description: 'Current playback and requests' }, { to: '/integrations', label: 'Integrations', icon: Plug, description: 'Plex, Jellyfin and source addons', end: true }] },
  { label: 'Discovery', links: [{ to: '/catalogs', label: 'Catalogs', icon: LibraryBig, description: 'Discovery rows and previews' }, { to: '/tagging', label: 'Tagging', icon: Tags, description: 'Enrichment and tagging' }, { to: '/tag-quality', label: 'Tag quality', icon: Activity, description: 'Tag observations and aliases' }] },
  { label: 'Operations', links: [{ to: '/jobs', label: 'Jobs & build', icon: Database, description: 'Background work and builds' }, { to: '/schedule', label: 'Schedule', icon: CalendarClock, description: 'Timed operations' }, { to: '/fingerprints', label: 'Fingerprinting', icon: Fingerprint, description: 'Skip marker analysis' }] },
  { label: 'System', links: [{ to: '/settings', label: 'Settings', icon: Settings, description: 'Workspace configuration' }, { to: '/diagnostics', label: 'Diagnostics', icon: Wrench, description: 'Pipeline health' }] },
];

// Installed addons with a settings UI get their own entries, built from the
// integrations list at runtime. Nothing here names a specific addon.
function useNavGroups(): NavGroup[] {
  const list = useQuery({ queryKey: ['admin-integrations'], queryFn: () => adminApi<IntegrationsResponse>('/integrations'), staleTime: 60_000 });
  return useMemo(() => {
    const addons = (list.data?.integrations ?? []).filter((item) => item.kind === 'addon' && item.settings_url);
    if (!addons.length) return groups;
    const addonGroup: NavGroup = { label: 'Addons', links: addons.map((addon) => ({ to: `/integrations/${encodeURIComponent(addon.id)}`, label: addon.label, icon: Blocks, description: 'Source addon settings' })) };
    const at = groups.findIndex((group) => group.label === 'Playback') + 1;
    return [...groups.slice(0, at), addonGroup, ...groups.slice(at)];
  }, [list.data]);
}

function toDestinations(navGroups: NavGroup[]) {
  return navGroups.flatMap((group) => group.links.map((link) => ({ ...link, group: group.label })));
}
function SearchButton({ onClick, compact = false }: { onClick: () => void; compact?: boolean }) {
  return <button type="button" className={`navSearch ${compact ? 'compact' : ''}`} onClick={onClick} aria-label={compact ? 'Search admin pages' : undefined}><Search size={17} /><span>Search pages</span>{!compact ? <kbd>⌘ K</kbd> : null}</button>;
}

function NavContent({ navGroups, onNavigate, onSearch, logout }: { navGroups: NavGroup[]; onNavigate: () => void; onSearch: () => void; logout: () => void }) {
  return <>
    <div className="brand"><img src="/static/logo.png" alt="" /><div><strong>Curatio</strong><span>Admin workspace</span></div></div>
    <SearchButton onClick={onSearch} />
    <nav aria-label="Admin pages">{navGroups.map((group) => <div className="navGroup" key={group.label}><span>{group.label}</span>{group.links.map((link) => <NavLink key={link.to} to={link.to} end={link.end} onClick={onNavigate}><link.icon size={17} aria-hidden="true" />{link.label}</NavLink>)}</div>)}</nav>
    <div className="sidebarFooter"><div className="accountRow"><span className="accountAvatar" aria-hidden="true">A</span><span><strong>Administrator</strong><small>Curatio workspace</small></span></div><button className="signOut" onClick={logout}><LogOut size={17} /> Sign out</button></div>
  </>;
}

function CommandPalette({ destinations, open, onOpenChange }: { destinations: ReturnType<typeof toDestinations>; open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const matches = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return needle ? destinations.filter((item) => `${item.label} ${item.group} ${item.description}`.toLocaleLowerCase().includes(needle)) : destinations;
  }, [query, destinations]);
  useEffect(() => { setActive(0); }, [query]);
  function select(path: string) {
    onOpenChange(false);
    setQuery('');
    navigate(path);
  }
  return <DialogPrimitive.Root open={open} onOpenChange={(value) => { onOpenChange(value); if (!value) setQuery(''); }}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialogOverlay" />
      <DialogPrimitive.Content className="commandPalette" onOpenAutoFocus={(event) => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; event.preventDefault(); inputRef.current?.focus(); }} onCloseAutoFocus={(event) => {
        event.preventDefault();
        queueMicrotask(() => { (returnFocus.current?.isConnected ? returnFocus.current : document.getElementById('admin-content'))?.focus(); });
      }}>
        <DialogPrimitive.Title className="srOnly">Search admin pages</DialogPrimitive.Title>
        <DialogPrimitive.Description className="srOnly">Search and open an admin destination.</DialogPrimitive.Description>
        <div className="commandSearch"><Search size={19} aria-hidden="true" /><input ref={inputRef} role="combobox" aria-label="Search admin pages" aria-autocomplete="list" aria-controls="command-results" aria-expanded={open} aria-activedescendant={matches[active] ? `command-option-${active}` : undefined} placeholder="Search pages…" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => {
          if (event.key === 'ArrowDown') { event.preventDefault(); setActive((value) => matches.length ? (value + 1) % matches.length : 0); }
          if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => matches.length ? (value + matches.length - 1) % matches.length : 0); }
          if (event.key === 'Enter' && matches[active]) { event.preventDefault(); select(matches[active].to); }
        }} /><kbd>ESC</kbd></div>
        <div className="commandResults" id="command-results" role="listbox" aria-label="Admin destinations">{matches.length ? matches.map((item, index) => <button key={item.to} id={`command-option-${index}`} type="button" role="option" aria-selected={active === index} className={active === index ? 'active' : ''} onMouseEnter={() => setActive(index)} onClick={() => select(item.to)}><item.icon size={18} aria-hidden="true" /><span><strong>{item.label}</strong><small>{item.group} · {item.description}</small></span></button>) : <div className="commandEmpty">No pages match “{query}”.</div>}</div>
        <div className="commandFooter"><span><kbd>↑</kbd><kbd>↓</kbd> Navigate</span><span><kbd>↵</kbd> Open</span></div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}

function useScrollMemory() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const previous = useRef({ key: location.key, pathname: location.pathname });
  const positions = useRef(new Map<string, number>());
  useLayoutEffect(() => {
    const last = previous.current;
    if (last.key !== location.key) positions.current.set(last.key, window.scrollY);
    if (last.pathname !== location.pathname) {
      window.scrollTo(0, navigationType === 'POP' ? positions.current.get(location.key) ?? 0 : 0);
    }
    previous.current = { key: location.key, pathname: location.pathname };
  }, [location.key, location.pathname, navigationType]);
}

function Shell() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const location = useLocation();
  useScrollMemory();
  const navGroups = useNavGroups();
  const destinations = useMemo(() => toDestinations(navGroups), [navGroups]);
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setDrawerOpen(false);
        setPaletteOpen((value) => !value);
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);
  async function logout() {
    await fetch('/admin/api/logout', { method: 'POST', credentials: 'same-origin' });
    window.location.assign('/admin');
  }
  const current = destinations.find((item) => item.to === location.pathname);
  return <div className="adminApp">
    <a className="skipLink" href="#admin-content">Skip to content</a>
    <aside className="sidebar" aria-label="Admin navigation"><NavContent navGroups={navGroups} onNavigate={() => undefined} onSearch={() => setPaletteOpen(true)} logout={() => void logout()} /></aside>
    <header className="mobileHeader">
      <DialogPrimitive.Root open={drawerOpen} onOpenChange={setDrawerOpen}>
        <DialogPrimitive.Trigger asChild><button className="iconButton" aria-label="Open navigation"><Menu size={20} /></button></DialogPrimitive.Trigger>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="dialogOverlay mobileDrawerOverlay" />
          <DialogPrimitive.Content className="mobileDrawer">
            <DialogPrimitive.Title className="srOnly">Admin navigation</DialogPrimitive.Title>
            <DialogPrimitive.Description className="srOnly">Choose an admin page.</DialogPrimitive.Description>
            <DialogPrimitive.Close className="drawerClose iconButton" aria-label="Close navigation"><X size={20} /></DialogPrimitive.Close>
            <NavContent navGroups={navGroups} onNavigate={() => setDrawerOpen(false)} onSearch={() => { setDrawerOpen(false); setPaletteOpen(true); }} logout={() => void logout()} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
      <img src="/static/logo.png" alt="" /><strong>Curatio</strong><span className="mobileHeaderSpacer" /><SearchButton compact onClick={() => setPaletteOpen(true)} />
    </header>
    <main id="admin-content" className="adminContent" tabIndex={-1}><Suspense fallback={<><PageHeader title={current?.label ?? 'Loading'} description={current?.description ?? 'Loading workspace section.'} /><LoadingPanel label="Loading section" /></>}><Outlet /></Suspense></main>
    <CommandPalette destinations={destinations} open={paletteOpen} onOpenChange={setPaletteOpen} />
  </div>;
}

export default function AdminApp() {
  return <Routes><Route element={<Shell />}>
    <Route index element={<Navigate replace to="overview" />} />
    <Route path="overview" element={<OverviewPage />} />
    <Route path="users" element={<UsersPage />} /><Route path="invites" element={<InvitesPage />} /><Route path="sessions" element={<SessionsPage />} />
    <Route path="streams" element={<StreamsPage />} /><Route path="integrations" element={<IntegrationsPage />} /><Route path="integrations/:addonId" element={<AddonPage />} />
    <Route path="catalogs" element={<CatalogsPage />} /><Route path="tagging" element={<TaggingPage />} /><Route path="tag-quality" element={<TagQualityPage />} />
    <Route path="jobs" element={<JobsPage />} /><Route path="schedule" element={<SchedulePage />} /><Route path="fingerprints" element={<FingerprintsPage />} />
    <Route path="settings" element={<SettingsPage />} /><Route path="diagnostics" element={<DiagnosticsPage />} />
    <Route path="*" element={<Navigate replace to="overview" />} />
  </Route></Routes>;
}
