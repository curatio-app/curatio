export class AdminApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function adminApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/admin/api${path}`, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers ?? {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) window.location.assign('/admin');
    const detail = typeof data === 'object' && data
      ? String((data as { detail?: unknown }).detail ?? 'Request failed')
      : 'Request failed';
    throw new AdminApiError(detail, response.status);
  }
  return data as T;
}

export function postAdmin<T>(path: string, body?: unknown) {
  return adminApi<T>(path, {
    method: 'POST',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

export function deleteAdmin<T>(path: string) {
  return adminApi<T>(path, { method: 'DELETE' });
}

export function formatDate(value?: string | null) {
  if (!value) return 'Never';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function formatDuration(seconds?: number | null) {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}
