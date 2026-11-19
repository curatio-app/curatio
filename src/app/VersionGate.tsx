import { useEffect, useState, type ReactNode } from 'react';
import { RefreshCw } from 'lucide-react';
import { fetchApiVersion } from '../api/accountClient';
import { EmptyState } from '../components/EmptyState';

/** The client API version this build speaks (docs/playback-api.md). */
export const CLIENT_API_VERSION = 2;

export const VERSION_MISMATCH_MESSAGE = "Your Curatio app and server versions don't match. Reload after updating.";

type GateState = 'checking' | 'ok' | 'mismatch';

/**
 * Launch handshake: `GET /api/version`. A 404 or an `api_version` other than
 * the one this build speaks stops the app here, before any other request. A
 * network hiccup does not block start-up (the app's own requests report it).
 */
export function VersionGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>('checking');

  useEffect(() => {
    const controller = new AbortController();
    fetchApiVersion(controller.signal)
      .then((info) => {
        if (controller.signal.aborted) return;
        setState(info && Number(info.api_version) === CLIENT_API_VERSION ? 'ok' : 'mismatch');
      })
      .catch(() => {
        if (!controller.signal.aborted) setState('ok');
      });
    return () => controller.abort();
  }, []);

  if (state === 'checking') return null;
  if (state === 'mismatch') {
    return (
      <main role="alert" style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 16 }}>
        <EmptyState
          tone="error"
          icon={RefreshCw}
          title="Update Required"
          message={VERSION_MISMATCH_MESSAGE}
          actionLabel="Reload"
          onAction={() => window.location.reload()}
        />
      </main>
    );
  }
  return <>{children}</>;
}
