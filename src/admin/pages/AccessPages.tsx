import { FormEvent, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Plus, ShieldCheck, Trash2, UserRoundCheck, UserRoundX } from 'lucide-react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { adminApi, deleteAdmin, formatDate, postAdmin } from '../api';
import { Button, ConfirmButton, Dialog, EmptyPanel, ErrorPanel, Field, LoadingPanel, PageHeader, Panel, ReconnectNotice, SidePanel, StatusPill } from '../components';

interface User { id: number; display_name?: string; email?: string; is_active: boolean; is_admin: boolean; auth_source: string; passkey_count: number; totp_enabled: boolean; passkey_exempt_until?: string; bandwidth_tier: string; catalog_count: number; last_sync?: string; }
interface Invite { code: string; invite_url: string; label?: string; expires_at?: string; is_used: boolean; used_by?: string; }
interface Session { token: string; token_full: string; username?: string; type: string; short_code?: string; created_at?: string; expires_at?: string; }
const USERS_PER_PAGE = 50;

export function UsersPage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const search = params.get('q') ?? '';
  const requestedPage = Number(params.get('page') ?? '1');
  const page = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const selectedId = Number(params.get('user'));
  const panelOpen = params.has('user');
  const query = useQuery({ queryKey: ['admin-users'], queryFn: () => adminApi<User[]>('/users') });
  const mutate = useMutation({ mutationFn: ({ path, body }: { path: string; body?: unknown }) => postAdmin(path, body), onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-users'] }) });
  const remove = useMutation({ mutationFn: (id: number) => deleteAdmin(`/users/${id}`), onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-users'] }) });
  const filtered = useMemo(() => (query.data ?? []).filter((user) => `${user.display_name ?? ''} ${user.email ?? ''} ${user.auth_source}`.toLowerCase().includes(search.trim().toLowerCase())), [query.data, search]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / USERS_PER_PAGE));
  const visiblePage = Math.min(page, totalPages);
  const users = filtered.slice((visiblePage - 1) * USERS_PER_PAGE, visiblePage * USERS_PER_PAGE);
  const selected = query.data?.find((user) => user.id === selectedId);

  function updateListParam(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    if (key === 'q') next.delete('page');
    setParams(next, { replace: true });
  }
  function openUser(id: number) {
    const next = new URLSearchParams(params);
    next.set('user', String(id));
    navigate({ search: next.toString() }, { state: { panelFromList: true } });
  }
  function closeUser() {
    if ((location.state as { panelFromList?: boolean } | null)?.panelFromList) navigate(-1);
    else { const next = new URLSearchParams(params); next.delete('user'); navigate({ search: next.toString() }, { replace: true }); }
  }

  return <>
    <PageHeader title="Users" description="Manage viewer access, account recovery, and playback limits." />
    <ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />
    <Panel title="Viewer access" description="Inspect an account to change access or recovery settings." actions={<input className="searchInput" type="search" value={search} onChange={(event) => updateListParam('q', event.target.value)} placeholder="Search viewers" aria-label="Search viewers" />}>
      {query.isLoading && !query.data ? <LoadingPanel label="Loading viewers" /> : null}
      {query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.data ? <>
        {users.length ? <div className="tableWrap"><table><thead><tr><th>Viewer</th><th>Security</th><th>Playback</th><th>Last sync</th><th>Status</th><th><span className="srOnly">Actions</span></th></tr></thead><tbody>{users.map((user) => <tr key={user.id}>
          <td data-label="Viewer"><strong>{user.display_name || 'Profile'}</strong><small>{user.email || user.auth_source}</small></td>
          <td data-label="Security">{user.passkey_count} passkey{user.passkey_count === 1 ? '' : 's'}<small>{user.totp_enabled ? '2FA enabled' : 'No 2FA'}{user.is_admin ? ' · Admin' : ''}</small></td>
          <td data-label="Playback"><span className="capitalize">{user.bandwidth_tier} bandwidth</span><small>{user.catalog_count} catalogs</small></td>
          <td data-label="Last sync">{formatDate(user.last_sync)}</td>
          <td data-label="Status"><StatusPill tone={user.is_active ? 'good' : 'bad'}>{user.is_active ? 'Active' : 'Disabled'}</StatusPill></td>
          <td data-label="Actions"><Button variant="secondary" size="compact" onClick={() => openUser(user.id)} aria-label={`Inspect ${user.display_name || user.email || `profile ${user.id}`}`}>Inspect</Button></td>
        </tr>)}</tbody></table></div> : <EmptyPanel title={search ? 'No viewers found' : 'No viewers yet'} message={search ? 'Try a different search.' : 'Invited viewers will appear here.'} />}
        {filtered.length > USERS_PER_PAGE ? <div className="pagination"><Button variant="secondary" disabled={visiblePage <= 1} onClick={() => updateListParam('page', String(visiblePage - 1))}>Previous</Button><span>Page {visiblePage} of {totalPages} · {filtered.length} viewers</span><Button variant="secondary" disabled={visiblePage >= totalPages} onClick={() => updateListParam('page', String(visiblePage + 1))}>Next</Button></div> : null}
      </> : null}
    </Panel>
    <SidePanel open={panelOpen} title={selected?.display_name || selected?.email || 'Viewer details'} description={selected?.email || selected?.auth_source} onClose={closeUser}>
      {query.isLoading && !query.data ? <LoadingPanel label="Loading viewer" /> : null}
      {query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.data && !selected ? <EmptyPanel title="Viewer not found" message="This account may have been removed. Close this panel to return to the list." /> : null}
      {selected ? <div className="detailStack">
        <Panel title="Account"><div className="statusLine"><span>Status</span><StatusPill tone={selected.is_active ? 'good' : 'bad'}>{selected.is_active ? 'Active' : 'Disabled'}</StatusPill></div><div className="statusLine"><span>Role</span><strong>{selected.is_admin ? 'Administrator' : 'Viewer'}</strong></div><div className="statusLine"><span>Catalogs</span><strong>{selected.catalog_count}</strong></div><div className="statusLine"><span>Last sync</span><strong>{formatDate(selected.last_sync)}</strong></div>
          <Field label="Playback bandwidth"><select aria-label={`Bandwidth for ${selected.display_name || selected.email || `profile ${selected.id}`}`} value={selected.bandwidth_tier} disabled={mutate.isPending} onChange={(event) => mutate.mutate({ path: `/users/${selected.id}/bandwidth`, body: { tier: event.target.value } })}><option value="low">Low bandwidth</option><option value="high">High bandwidth</option></select></Field>
          <div className="formActions"><Button variant="secondary" disabled={mutate.isPending} onClick={() => mutate.mutate({ path: `/users/${selected.id}/toggle` })}>{selected.is_active ? <UserRoundX size={16} /> : <UserRoundCheck size={16} />}{selected.is_active ? 'Disable viewer' : 'Enable viewer'}</Button><Button variant="secondary" disabled={mutate.isPending} onClick={() => mutate.mutate({ path: `/users/${selected.id}/admin`, body: { is_admin: !selected.is_admin } })}><ShieldCheck size={16} />{selected.is_admin ? 'Remove admin' : 'Make admin'}</Button></div>
        </Panel>
        <Panel title="Security & recovery"><div className="statusLine"><span>Passkeys</span><strong>{selected.passkey_count}</strong></div><div className="statusLine"><span>Two-factor authentication</span><strong>{selected.totp_enabled ? 'Enabled' : 'Not enabled'}</strong></div><div className="statusLine"><span>Passkey exemption</span><strong>{selected.passkey_exempt_until ? formatDate(selected.passkey_exempt_until) : 'None'}</strong></div><div className="formActions">
          <ConfirmButton variant="secondary" disabled={!selected.totp_enabled || mutate.isPending} prompt="Disable 2FA for this account?" onConfirm={() => mutate.mutate({ path: `/users/${selected.id}/reset-2fa` })}>Reset 2FA</ConfirmButton>
          <ConfirmButton variant="secondary" disabled={!selected.passkey_count || mutate.isPending} prompt="Remove every passkey from this account?" onConfirm={() => mutate.mutate({ path: `/users/${selected.id}/reset-passkeys` })}>Reset passkeys</ConfirmButton>
          {selected.passkey_exempt_until ? <ConfirmButton variant="secondary" disabled={mutate.isPending} prompt="Clear this account's passkey exemption?" onConfirm={() => mutate.mutate({ path: `/users/${selected.id}/passkey-exemption`, body: { clear: true } })}>Clear exemption</ConfirmButton> : <ConfirmButton variant="secondary" disabled={mutate.isPending} prompt="Allow password-only access for this account for 7 days?" onConfirm={() => mutate.mutate({ path: `/users/${selected.id}/passkey-exemption`, body: { days: 7 } })}>7-day exemption</ConfirmButton>}
        </div></Panel>
        <Panel title="Delete viewer" className="warningPanel"><p className="panelFootnote">This permanently removes the viewer profile and its personal catalogs.</p><ConfirmButton variant="danger" disabled={remove.isPending} prompt="Permanently delete this viewer profile and its personal catalogs?" onConfirm={() => remove.mutate(selected.id, { onSuccess: closeUser })}><Trash2 size={16} /> Delete profile</ConfirmButton></Panel>
        {mutate.error || remove.error ? <p className="formError" role="alert">{(mutate.error || remove.error)?.message}</p> : null}
      </div> : null}
    </SidePanel>
  </>;
}

export function InvitesPage() {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState('');
  const [hours, setHours] = useState('');
  const [copyNotice, setCopyNotice] = useState('');
  const query = useQuery({ queryKey: ['admin-invites'], queryFn: () => adminApi<Invite[]>('/invites') });
  const create = useMutation({ mutationFn: () => postAdmin<Invite>('/invites', { label, expires_hours: hours ? Number(hours) : null }), onSuccess: () => { setLabel(''); setHours(''); setOpen(false); void client.invalidateQueries({ queryKey: ['admin-invites'] }); } });
  const remove = useMutation({ mutationFn: (code: string) => deleteAdmin(`/invites/${encodeURIComponent(code)}`), onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-invites'] }) });
  function submit(event: FormEvent) { event.preventDefault(); create.mutate(); }
  async function copyInvite(invite: Invite) { try { await navigator.clipboard.writeText(invite.invite_url); setCopyNotice('Invitation link copied.'); } catch { setCopyNotice('Could not copy the link. Please try again.'); } }
  return <>
    <PageHeader title="Invites" description="Create one-time links and see who has used them." actions={<Button onClick={() => setOpen(true)}><Plus size={16} /> Create invite</Button>} />
    <ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />
    {copyNotice ? <p className="globalNotice" role="status">{copyNotice}</p> : null}
    <Panel title="Invitation history">
      {query.isLoading && !query.data ? <LoadingPanel label="Loading invitations" /> : null}
      {query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.data ? query.data.length ? <div className="tableWrap"><table><thead><tr><th>Label</th><th>Code</th><th>Expiration</th><th>Status</th><th><span className="srOnly">Actions</span></th></tr></thead><tbody>{query.data.map((invite) => <tr key={invite.code}><td data-label="Label">{invite.label || 'Unlabeled invite'}</td><td data-label="Code"><code>{invite.code.slice(0, 10)}…</code></td><td data-label="Expiration">{invite.expires_at ? formatDate(invite.expires_at) : 'Never'}</td><td data-label="Status"><StatusPill tone={invite.is_used ? 'neutral' : 'good'}>{invite.is_used ? `Used${invite.used_by ? ` by ${invite.used_by}` : ''}` : 'Available'}</StatusPill></td><td data-label="Actions"><div className="rowActions"><Button variant="secondary" size="compact" aria-label={`Copy invitation ${invite.label || invite.code}`} onClick={() => void copyInvite(invite)}><Copy size={16} /></Button><ConfirmButton variant="danger" size="compact" aria-label={`Delete invitation ${invite.label || invite.code}`} prompt="Delete this invitation?" onConfirm={() => remove.mutate(invite.code)}><Trash2 size={16} /></ConfirmButton></div></td></tr>)}</tbody></table></div> : <EmptyPanel title="No invitations" message="Create an invitation to bring in your first viewer." /> : null}
      {remove.error ? <p className="formError" role="alert">{remove.error.message}</p> : null}
    </Panel>
    <Dialog open={open} onOpenChange={setOpen} title="Create invitation" description="Create a one-time link for a new viewer."><form className="stackForm" onSubmit={submit}><Field label="Label"><input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Family, beta tester…" /></Field><Field label="Expires after"><select value={hours} onChange={(event) => setHours(event.target.value)}><option value="">Never</option><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option></select></Field><div className="formActions"><Button disabled={create.isPending}><Plus size={16} />{create.isPending ? 'Creating…' : 'Create invite'}</Button><Button type="button" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button></div>{create.error ? <p className="formError" role="alert">{create.error.message}</p> : null}</form></Dialog>
  </>;
}

export function SessionsPage() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['admin-sessions'], queryFn: () => adminApi<{ web_sessions: Session[]; pairing_sessions: Session[]; device_sessions: Session[] }>('/sessions') });
  const revoke = useMutation({ mutationFn: (session: Session) => deleteAdmin(`/sessions/${session.type === 'web' ? 'web' : 'pairing'}/${encodeURIComponent(session.token_full)}`), onSuccess: () => void client.invalidateQueries({ queryKey: ['admin-sessions'] }) });
  const sessions = query.data ? [...query.data.web_sessions, ...query.data.pairing_sessions, ...query.data.device_sessions] : [];
  return <>
    <PageHeader title="Sessions" description="Review signed-in browsers and pending device pairings." />
    <ReconnectNotice reconnecting={Boolean(query.error && query.data)} onRetry={() => void query.refetch()} />
    <Panel title="Active sessions" description="Revoke access when a browser or pairing request is no longer trusted.">
      {query.isLoading && !query.data ? <LoadingPanel label="Loading sessions" /> : null}
      {query.error && !query.data ? <ErrorPanel error={query.error} onRetry={() => void query.refetch()} /> : null}
      {query.data ? sessions.length ? <div className="tableWrap"><table><thead><tr><th>Type</th><th>Viewer or code</th><th>Created</th><th>Expires</th><th><span className="srOnly">Actions</span></th></tr></thead><tbody>{sessions.map((session) => <tr key={`${session.type}-${session.token_full}`}><td data-label="Type"><StatusPill>{session.type}</StatusPill></td><td data-label="Viewer or code">{session.username || session.short_code || session.token}</td><td data-label="Created">{formatDate(session.created_at)}</td><td data-label="Expires">{formatDate(session.expires_at)}</td><td data-label="Actions">{session.type !== 'device' ? <ConfirmButton variant="danger" size="compact" disabled={revoke.isPending} prompt="Revoke this session now?" onConfirm={() => revoke.mutate(session)}><Trash2 size={16} /> Revoke</ConfirmButton> : <span className="muted">Pending</span>}</td></tr>)}</tbody></table></div> : <EmptyPanel title="No active sessions" message="Signed-in browsers and pairing requests will appear here." /> : null}
      {revoke.error ? <p className="formError" role="alert">{revoke.error.message}</p> : null}
    </Panel>
  </>;
}
