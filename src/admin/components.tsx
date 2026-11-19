import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, PropsWithChildren, ReactNode } from 'react';
import { forwardRef, useRef } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { useSearchParams } from 'react-router-dom';
import { AlertTriangle, LoaderCircle, RefreshCw, WifiOff, X } from 'lucide-react';

export function PageHeader({ title, description, actions }: { title: string; description: string; actions?: ReactNode }) {
  return (
    <header className="pageHeader">
      <div><p className="eyebrow">Curatio control center</p><h1>{title}</h1><p>{description}</p></div>
      {actions ? <div className="pageActions">{actions}</div> : null}
    </header>
  );
}

export function Panel({ title, description, actions, children, className = '' }: PropsWithChildren<{ title?: string; description?: string; actions?: ReactNode; className?: string }>) {
  return (
    <section className={`panel ${className}`}>
      {title || actions ? <div className="panelHeader"><div>{title ? <h2>{title}</h2> : null}{description ? <p>{description}</p> : null}</div>{actions}</div> : null}
      {children}
    </section>
  );
}

export function StatusPill({ tone = 'neutral', children }: PropsWithChildren<{ tone?: 'good' | 'warn' | 'bad' | 'neutral' }>) {
  return <span className={`statusPill ${tone}`}><span aria-hidden="true" />{children}</span>;
}

export function LoadingPanel({ label = 'Loading' }: { label?: string }) {
  return <div className="statePanel" role="status"><LoaderCircle className="spin" /><span>{label}</span></div>;
}

export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return <div className="statePanel error" role="alert"><AlertTriangle /><div><strong>Couldn’t load this section</strong><p>{error instanceof Error ? error.message : 'Unknown error'}</p></div>{onRetry ? <button type="button" className="button secondary" onClick={onRetry}><RefreshCw size={16} /> Retry</button> : null}</div>;
}

export function EmptyPanel({ title, message }: { title: string; message: string }) {
  return <div className="emptyPanel"><strong>{title}</strong><p>{message}</p></div>;
}

export function ReconnectNotice({ reconnecting, onRetry }: { reconnecting: boolean; onRetry?: () => void }) {
  if (!reconnecting) return null;
  return <div className="reconnectNotice" role="status"><WifiOff size={17} /><span>Connection interrupted. Showing the last available data while we reconnect.</span>{onRetry ? <Button variant="secondary" size="compact" onClick={onRetry}><RefreshCw size={15} /> Retry now</Button> : null}</div>;
}

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';
export type ButtonSize = 'default' | 'compact';
export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ className = '', variant = 'primary', size = 'default', ...props }, ref) {
  const classes = ['button', variant, size === 'compact' ? 'compact' : '', className].filter(Boolean).join(' ');
  return <button {...props} ref={ref} className={classes} />;
});

export function ConfirmButton({ prompt, onConfirm, children, ...props }: PropsWithChildren<ButtonProps & { prompt: string; onConfirm: () => void }>) {
  return <DialogPrimitive.Root>
    <DialogPrimitive.Trigger asChild><Button {...props}>{children}</Button></DialogPrimitive.Trigger>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialogOverlay" />
      <DialogPrimitive.Content className="dialogContent confirmDialog">
        <div className="dialogHeader"><div><DialogPrimitive.Title>Confirm action</DialogPrimitive.Title><DialogPrimitive.Description>{prompt}</DialogPrimitive.Description></div><DialogPrimitive.Close className="iconButton" aria-label="Close dialog"><X size={18} /></DialogPrimitive.Close></div>
        <div className="dialogFooter"><DialogPrimitive.Close asChild><Button variant="secondary">Cancel</Button></DialogPrimitive.Close><DialogPrimitive.Close asChild><Button variant={props.variant === 'danger' ? 'danger' : 'primary'} onClick={onConfirm}>Confirm</Button></DialogPrimitive.Close></div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}

export function StatGrid({ items }: { items: Array<{ label: string; value: ReactNode; hint?: string }> }) {
  return <div className="statGrid">{items.map((item) => <article className="statCard" key={item.label}><span>{item.label}</span><strong>{item.value}</strong>{item.hint ? <small>{item.hint}</small> : null}</article>)}</div>;
}

export function Field({ label, hint, children }: PropsWithChildren<{ label: string; hint?: string }>) {
  return <label className="field"><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label>;
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(props, ref) {
  return <input {...props} ref={ref} />;
});

export type DialogProps = PropsWithChildren<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  footer?: ReactNode;
}>;

export function Dialog({ open, onOpenChange, title, description, children, footer }: DialogProps) {
  return <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialogOverlay" />
      <DialogPrimitive.Content className="dialogContent">
        <div className="dialogHeader"><div><DialogPrimitive.Title>{title}</DialogPrimitive.Title><DialogPrimitive.Description className={description ? undefined : 'srOnly'}>{description || 'Use the controls in this dialog to complete the task.'}</DialogPrimitive.Description></div><DialogPrimitive.Close className="iconButton" aria-label="Close dialog"><X size={18} /></DialogPrimitive.Close></div>
        <div className="dialogBody">{children}</div>
        {footer ? <div className="dialogFooter">{footer}</div> : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}

export type SidePanelProps = PropsWithChildren<{
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  footer?: ReactNode;
}>;

export function SidePanel({ open, title, description, onClose, children, footer }: SidePanelProps) {
  const returnFocus = useRef<HTMLElement | null>(null);
  return <DialogPrimitive.Root open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialogOverlay" />
      <DialogPrimitive.Content className="sidePanel" onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }} onCloseAutoFocus={(event) => {
        event.preventDefault();
        queueMicrotask(() => {
          const target = returnFocus.current?.isConnected ? returnFocus.current : document.getElementById('admin-content');
          target?.focus();
        });
      }}>
        <div className="sidePanelHeader"><div><DialogPrimitive.Title>{title}</DialogPrimitive.Title><DialogPrimitive.Description className={description ? undefined : 'srOnly'}>{description || 'Inspect details and return to the underlying page when finished.'}</DialogPrimitive.Description></div><DialogPrimitive.Close className="iconButton" aria-label="Close panel"><X size={18} /></DialogPrimitive.Close></div>
        <div className="sidePanelBody">{children}</div>
        {footer ? <div className="sidePanelFooter">{footer}</div> : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}

export type TabOption = { value: string; label: string };
export function Tabs({ tabs, value, onValueChange, children }: {
  tabs: TabOption[];
  value: string;
  onValueChange: (value: string) => void;
  children: ReactNode;
}) {
  return <TabsPrimitive.Root value={value} onValueChange={onValueChange}>
    <TabsPrimitive.List className="pageTabs" aria-label="Sections">
      {tabs.map((tab) => <TabsPrimitive.Trigger className="pageTab" key={tab.value} value={tab.value}>{tab.label}</TabsPrimitive.Trigger>)}
    </TabsPrimitive.List>
    <TabsPrimitive.Content className="tabContent" value={value}>{children}</TabsPrimitive.Content>
  </TabsPrimitive.Root>;
}

export function PageTabs({ tabs, param = 'tab', defaultTab, children }: {
  tabs: TabOption[];
  param?: string;
  defaultTab: string;
  children: (activeTab: string) => ReactNode;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get(param);
  const active = tabs.some((tab) => tab.value === requested) ? requested! : defaultTab;
  return <Tabs tabs={tabs} value={active} onValueChange={(value) => {
    const next = new URLSearchParams(searchParams);
    if (value === defaultTab) next.delete(param); else next.set(param, value);
    setSearchParams(next, { preventScrollReset: true });
  }}>{children(active)}</Tabs>;
}

export function Notice({ children, tone = 'neutral', ...props }: PropsWithChildren<HTMLAttributes<HTMLDivElement> & { tone?: 'neutral' | 'good' | 'warn' | 'bad' }>) {
  return <div {...props} className={`notice ${tone} ${props.className ?? ''}`}>{children}</div>;
}
