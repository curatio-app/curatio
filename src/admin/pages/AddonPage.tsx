import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink } from 'lucide-react';
import { adminApi } from '../api';
import { EmptyPanel, ErrorPanel, LoadingPanel, PageHeader } from '../components';
import type { IntegrationsResponse } from './IntegrationsPage';

// Hosts an installed addon's own settings UI inside the admin shell. Curatio
// knows nothing about any particular addon: the label, page and API all come
// from the addon at runtime. The page is served same-origin at the addon's
// settings_url and loaded with ?embed=1. An addon may report its content
// height with postMessage({ type: 'curatio:addon-height', height }) so the
// frame grows instead of scrolling.

export const ADDON_HEIGHT_MESSAGE = 'curatio:addon-height';
const MIN_HEIGHT = 480;

export function embedUrl(settingsUrl: string) {
  return `${settingsUrl}${settingsUrl.includes('?') ? '&' : '?'}embed=1`;
}

export default function AddonPage() {
  const { addonId = '' } = useParams();
  const list = useQuery({ queryKey: ['admin-integrations'], queryFn: () => adminApi<IntegrationsResponse>('/integrations') });
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(MIN_HEIGHT);
  const addon = list.data?.integrations.find((item) => item.kind === 'addon' && item.id === addonId);

  useEffect(() => {
    setHeight(MIN_HEIGHT);
    function onMessage(event: MessageEvent) {
      if (event.origin !== window.location.origin || event.source !== frame.current?.contentWindow) return;
      const data = event.data as { type?: unknown; height?: unknown };
      if (data?.type === ADDON_HEIGHT_MESSAGE && typeof data.height === 'number' && Number.isFinite(data.height)) {
        setHeight(Math.max(MIN_HEIGHT, Math.ceil(data.height)));
      }
    }
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [addonId]);

  const back = <Link className="button secondary" to="/integrations"><ArrowLeft size={14} /> Integrations</Link>;
  if (list.isLoading) return <><PageHeader title="Addon" description="Loading addon settings." actions={back} /><LoadingPanel label="Loading addon" /></>;
  if (list.error) return <><PageHeader title="Addon" description="Source addon settings." actions={back} /><ErrorPanel error={list.error} onRetry={() => void list.refetch()} /></>;
  if (!addon || !addon.settings_url) {
    return <><PageHeader title="Addon" description="Source addon settings." actions={back} /><EmptyPanel title="Addon not available" message={addon ? 'This addon has no settings page.' : 'No installed addon matches this address.'} /></>;
  }
  return <>
    <PageHeader
      title={addon.label}
      description="Source addon settings"
      actions={<>{back}<a className="button secondary" href={addon.settings_url} target="_blank" rel="noreferrer">Open alone <ExternalLink size={14} /></a></>}
    />
    <iframe ref={frame} key={addon.id} className="addonFrame" title={`${addon.label} settings`} src={embedUrl(addon.settings_url)} style={{ height }} />
  </>;
}
