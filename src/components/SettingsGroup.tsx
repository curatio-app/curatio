import { ChevronRight } from 'lucide-react';
import type { ReactNode } from 'react';
import styles from './SettingsGroup.module.css';

export type SettingTint = 'blue' | 'purple' | 'green' | 'orange' | 'red' | 'gray';

const TINT_CLASS: Record<SettingTint, string> = {
  blue: styles.tintBlue,
  purple: styles.tintPurple,
  green: styles.tintGreen,
  orange: styles.tintOrange,
  red: styles.tintRed,
  gray: styles.tintGray,
};

export function SettingIcon({ icon, tint }: { icon: ReactNode; tint: SettingTint }) {
  return <span className={`${styles.icon} ${TINT_CLASS[tint]}`}>{icon}</span>;
}

export function SettingsGroup({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <section className={styles.group}>
      {label ? <h2 className={styles.groupLabel}>{label}</h2> : null}
      <div className={styles.rows}>{children}</div>
    </section>
  );
}

interface BaseRowProps {
  icon: ReactNode;
  tint: SettingTint;
  label: string;
}

export function SettingsRow({
  icon,
  tint,
  label,
  value,
  href,
  onClick,
}: BaseRowProps & { value?: string | null; href?: string; onClick?: () => void }) {
  const content = (
    <>
      <SettingIcon icon={icon} tint={tint} />
      <span className={styles.label}>{label}</span>
      {value ? <span className={styles.value}>{value}</span> : null}
      {href || onClick ? <ChevronRight size={16} className={styles.chevron} /> : null}
    </>
  );
  if (href) {
    return (
      <div data-nav-row={`setting:${label}`}>
        <a className={`${styles.row} ${styles.interactive}`} href={href} data-nav-item data-own-focus>{content}</a>
      </div>
    );
  }
  if (onClick) {
    return (
      <div data-nav-row={`setting:${label}`}>
        <button className={`${styles.row} ${styles.interactive}`} type="button" onClick={onClick} data-nav-item data-own-focus>
          {content}
        </button>
      </div>
    );
  }
  return <div className={styles.row}>{content}</div>;
}

export function SettingsToggleRow({
  icon,
  tint,
  label,
  checked,
  onChange,
}: BaseRowProps & { checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <div className={`${styles.row} ${styles.toggleRow}`} data-nav-row={`setting:${label}`}>
      <SettingIcon icon={icon} tint={tint} />
      <span className={styles.label}>{label}</span>
      <button
        type="button"
        className={`${styles.toggle} ${checked ? styles.toggleOn : ''}`}
        role="switch"
        aria-checked={checked}
        aria-label={label}
        data-nav-item
        data-own-focus
        onClick={() => onChange(!checked)}
      >
        <span className={styles.knob} />
      </button>
    </div>
  );
}

export function SettingsSelectRow({
  icon,
  tint,
  label,
  value,
  options,
  onChange,
}: BaseRowProps & {
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  const id = `setting-${label.replace(/\s+/g, '-').toLowerCase()}`;
  return (
    <div className={styles.row} data-nav-row={`setting:${label}`}>
      <SettingIcon icon={icon} tint={tint} />
      <label className={styles.label} htmlFor={id}>{label}</label>
      <select id={id} className={styles.select} value={value} onChange={(event) => onChange(event.target.value)} data-nav-item>
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </div>
  );
}

export function SettingsStatusRow({
  icon,
  tint,
  label,
  sub,
  connected,
}: BaseRowProps & { sub?: string | null; connected?: boolean }) {
  return (
    <div className={styles.row}>
      <SettingIcon icon={icon} tint={tint} />
      <span className={styles.statusText}>
        <span className={styles.label}>{label}</span>
        {sub ? <small className={styles.sub}>{sub}</small> : null}
      </span>
      <span className={`${styles.dot} ${connected ? styles.dotOn : ''}`} aria-hidden="true" />
    </div>
  );
}
