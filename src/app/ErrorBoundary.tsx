import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { EmptyState } from '../components/EmptyState';
import { isChunkLoadError } from './lazyWithRetry';

interface Props {
  children: ReactNode;
  /** Shown as the title of the fallback. */
  title?: string;
  /** Extra action, e.g. "Go back" for an overlay layer. */
  onDismiss?: () => void;
  dismissLabel?: string;
  /** Changing this clears a caught error (e.g. a new item in the same layer). */
  resetKey?: unknown;
  /** Cover the viewport (overlay layers). */
  fullscreen?: boolean;
}

interface State {
  error: Error | null;
}

/**
 * Keeps one broken screen from blanking the whole app. The root boundary
 * catches anything; each overlay layer has its own so a bad Detail page can be
 * closed without losing Home underneath.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[curatio] render error', error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  private retry = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    const { title = 'Something went wrong', onDismiss, dismissLabel = 'Go back', fullscreen } = this.props;
    // A page whose code could not be downloaded stays broken until the app is
    // loaded again (React keeps the failed import): offer exactly that.
    const stale = isChunkLoadError(this.state.error);
    const body = (
      <EmptyState
        tone="error"
        icon={AlertTriangle}
        title={stale ? "Couldn't load this page" : title}
        message={stale ? 'Check your connection, then reload Curatio.' : 'This part of Curatio hit a problem. Trying again usually fixes it.'}
        actionLabel={stale ? 'Reload' : onDismiss ? dismissLabel : 'Try again'}
        onAction={stale ? () => window.location.reload() : onDismiss ?? this.retry}
      />
    );
    if (!fullscreen) return body;
    // Overlay layers are full-screen pages; their fallback must cover what's below.
    return (
      <div
        role="alert"
        style={{ position: 'fixed', inset: 0, display: 'grid', placeItems: 'center', background: 'var(--canvas)', zIndex: 1 }}
      >
        {body}
      </div>
    );
  }
}
