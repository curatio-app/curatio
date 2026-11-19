import { AppShell } from './app/AppShell';
import { ErrorBoundary } from './app/ErrorBoundary';
import { VersionGate } from './app/VersionGate';

export default function App() {
  return (
    <ErrorBoundary fullscreen title="Curatio hit a problem">
      <VersionGate>
        <AppShell />
      </VersionGate>
    </ErrorBoundary>
  );
}
