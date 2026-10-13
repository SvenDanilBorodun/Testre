// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// Shared design-system primitives used by redesigned student pages.
//
// Shared with the teacher web build: every component keeps its Tailwind classes
// and carries stable hook classes (`eb-btn`, `eb-card__title`, …) that the
// student app's „Sucher" look restyles under `:root[data-ui='sucher']`
// (components/sucher/sucher.css, paint only). The web build never sets that
// attribute, so it renders exactly as before — except Btn's pointer-focus
// release, which applies to both builds (owner decision D11).

import React from 'react';
import clsx from 'clsx';
import Icon from './icons/Icon';
import { releasePointerFocus } from './sucher/pointerFocus';

export const Pill = ({ tone = 'neutral', children, dot, className }) => {
  const toneMap = {
    neutral: 'bg-[var(--bg-sunk)] text-[var(--ink-2)] border-[var(--line)]',
    accent: 'bg-[var(--accent-wash)] text-[var(--accent-ink)] border-transparent',
    success: 'bg-[var(--success-wash)] text-[color:var(--success)] border-transparent',
    danger: 'bg-[var(--danger-wash)] text-[color:var(--danger)] border-transparent',
    amber: 'bg-[var(--amber-wash)] text-[color:var(--amber)] border-transparent',
    glass: 'bg-black/40 text-white border-white/15 backdrop-blur-md',
  };
  return (
    <span
      className={clsx(
        'eb-pill',
        `eb-pill--${toneMap[tone] ? tone : 'neutral'}`,
        'inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 border rounded-full whitespace-nowrap',
        toneMap[tone] || toneMap.neutral,
        className
      )}
    >
      {dot && (
        <span className="eb-pill__dot w-1.5 h-1.5 rounded-full" style={{ background: 'currentColor' }} />
      )}
      {children}
    </span>
  );
};

export const Btn = ({ variant = 'ghost', size = 'md', children, className, onClick, ...rest }) => {
  const sizes = {
    sm: 'h-8 px-3 text-xs',
    md: 'h-10 px-4 text-sm',
    lg: 'h-12 px-5 text-sm',
  };
  const variants = {
    primary:
      'bg-[var(--accent)] text-white hover:brightness-110 active:brightness-95 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed',
    secondary:
      'bg-white text-[var(--ink)] border border-[var(--line)] hover:bg-[var(--bg-sunk)] disabled:opacity-50 disabled:cursor-not-allowed',
    ghost:
      'bg-transparent text-[var(--ink-2)] hover:bg-[var(--bg-sunk)] border border-transparent disabled:opacity-50 disabled:cursor-not-allowed',
    danger:
      'bg-[var(--danger)] text-white hover:brightness-110 border border-transparent disabled:opacity-50 disabled:cursor-not-allowed',
    dark:
      'bg-white/8 text-white/90 border border-white/15 hover:bg-white/15 backdrop-blur-md',
  };
  return (
    <button
      {...rest}
      // A mouse click gives the focus back to the page; a keyboard click keeps
      // it (D11, both builds).
      onClick={(e) => {
        releasePointerFocus(e);
        if (onClick) onClick(e);
      }}
      className={clsx(
        'eb-btn',
        `eb-btn--${variant}`,
        `eb-btn--${size}`,
        'inline-flex items-center gap-1.5 font-medium rounded-[var(--radius-sm)] transition',
        sizes[size],
        variants[variant],
        className
      )}
    >
      {children}
    </button>
  );
};

// `titleStyle`: 'sans' (default) or 'serif' — the serif title is a Sucher look
// only (the web build has no rule for its hook class).
export const Card = ({ title, subtitle, children, className, right, padded = true, titleStyle = 'sans' }) => (
  <section
    className={clsx(
      'eb-card',
      'bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-soft',
      className
    )}
  >
    {(title || right) && (
      <header className="eb-card__head flex items-center justify-between gap-3 px-5 pt-4 pb-3 border-b border-[var(--line)]">
        <div className="min-w-0">
          {title && (
            <h3
              className={clsx(
                'eb-card__title',
                titleStyle === 'serif' && 'eb-card__title--serif',
                'text-sm font-semibold text-[var(--ink)] tracking-tight'
              )}
            >
              {title}
            </h3>
          )}
          {subtitle && <p className="eb-card__sub text-xs text-[var(--ink-3)] mt-0.5">{subtitle}</p>}
        </div>
        {right}
      </header>
    )}
    <div className={clsx('eb-card__body', padded && 'p-5')}>{children}</div>
  </section>
);

export const Stat = ({ label, value, tone, trend, className }) => (
  <div className={clsx('eb-stat', 'flex flex-col', className)}>
    <span className="eb-stat__label text-[11px] font-semibold tracking-wide uppercase text-[var(--ink-3)]">
      {label}
    </span>
    <span
      className={clsx(
        'eb-stat__value',
        'font-mono font-semibold text-[22px] leading-none mt-1.5',
        tone === 'success' && 'text-[color:var(--success)]',
        tone === 'danger' && 'text-[color:var(--danger)]',
        tone === 'accent' && 'text-[var(--accent-ink)]'
      )}
    >
      {value}
    </span>
    {trend && <span className="eb-stat__trend text-[11px] text-[var(--ink-3)] mt-1">{trend}</span>}
  </div>
);

export const LogoMark = ({ size = 22 }) => (
  <span
    className="inline-flex items-center justify-center rounded-[8px]"
    style={{ width: size + 4, height: size + 4, background: 'var(--ink)' }}
  >
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <rect x="5" y="8" width="14" height="10" rx="2.5" stroke="white" strokeWidth="1.8" />
      <circle cx="9.5" cy="13" r="1.3" fill="var(--accent)" />
      <circle cx="14.5" cy="13" r="1.3" fill="white" />
      <path d="M12 8V4.5" stroke="white" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="12" cy="3.6" r="1.1" fill="var(--accent)" />
    </svg>
  </span>
);

// `size`: 'xl' (default) | 'lg' | 'md' | 'sm' — the Sucher serif title's size.
const SECTION_SIZES = new Set(['xl', 'lg', 'md', 'sm']);

export const SectionHeader = ({ eyebrow, title, description, right, className, size = 'xl' }) => (
  <div
    className={clsx(
      'eb-section',
      `eb-section--${SECTION_SIZES.has(size) ? size : 'xl'}`,
      'flex items-start justify-between gap-6',
      className
    )}
  >
    <div className="min-w-0">
      {eyebrow && (
        <div className="eb-section__eyebrow text-xs font-mono text-[var(--ink-3)] uppercase tracking-wider">
          {eyebrow}
        </div>
      )}
      {title && (
        <h1 className="eb-section__title text-[28px] font-semibold tracking-tight mt-1 leading-[1.2] text-[var(--ink)]">
          {title}
        </h1>
      )}
      {description && (
        <p className="eb-section__desc text-[15px] text-[var(--ink-3)] mt-2">{description}</p>
      )}
    </div>
    {right && <div className="shrink-0 pt-1">{right}</div>}
  </div>
);

export const Progress = ({ pct = 0, tone = 'accent', className }) => (
  <div
    className={clsx(
      'eb-progress',
      'h-1.5 w-full bg-[var(--bg-sunk)] rounded-full overflow-hidden',
      className
    )}
  >
    <div
      className="eb-progress__fill h-full rounded-full transition-[width] duration-500"
      style={{
        width: `${Math.max(0, Math.min(100, pct))}%`,
        background:
          tone === 'danger'
            ? 'var(--danger)'
            : tone === 'success'
            ? 'var(--success)'
            : 'var(--accent)',
      }}
    />
  </div>
);

export const StatBig = ({ label, value, sub, tone, className }) => (
  <div className={clsx('eb-stat', 'flex flex-col shrink-0', className)}>
    <div className="eb-stat__label text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
      {label}
    </div>
    <div
      className={clsx(
        'eb-stat__value',
        'font-mono text-[28px] font-semibold leading-none mt-1.5',
        tone === 'success' && 'text-[color:var(--success)]',
        tone === 'danger' && 'text-[color:var(--danger)]',
        tone === 'amber' && 'text-[color:var(--amber)]'
      )}
    >
      {value}
    </div>
    {sub && <div className="eb-stat__trend text-[11px] text-[var(--ink-3)] mt-1">{sub}</div>}
  </div>
);

export const Divider = ({ className }) => (
  <div className={clsx('eb-divider', 'w-px h-12 bg-[var(--line)] shrink-0', className)} />
);

export const Avatar = ({ name, size = 36, className }) => {
  const initials = (name || '?')
    .split(' ')
    .map((s) => s[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase();
  const hues = ['#0A7F7A', '#2461C7', '#6A46C7', '#C74660', '#D08A1E', '#2F9E63'];
  const hash =
    (name || '').split('').reduce((a, c) => a + c.charCodeAt(0), 0) % hues.length;
  return (
    <span
      className={clsx(
        'eb-avatar',
        'inline-flex items-center justify-center rounded-full text-white font-semibold shrink-0',
        className
      )}
      style={{
        width: size,
        height: size,
        // The Sucher look sets --eb-avatar-bg; the web build falls back to the hue.
        background: `var(--eb-avatar-bg, ${hues[hash]})`,
        fontSize: Math.round(size * 0.35),
      }}
    >
      {initials}
    </span>
  );
};

export const TopBar = ({
  title,
  subtitle,
  roleBadge,
  user,
  userSub,
  userName,
  onLogout,
  icon,
}) => (
  <header className="bg-white border-b border-[var(--line)] px-8 h-[64px] shrink-0 flex items-center justify-between">
    <div className="flex items-center gap-3 min-w-0">
      {icon || <LogoMark />}
      <div className="min-w-0">
        <div className="text-sm font-semibold tracking-tight text-[var(--ink)] truncate">
          {title || 'EduBotics'}
        </div>
        {subtitle && (
          <div className="text-[11px] font-mono text-[var(--ink-3)] -mt-0.5 truncate">
            {subtitle}
          </div>
        )}
      </div>
    </div>
    <div className="flex items-center gap-4 shrink-0">
      {roleBadge}
      {user && (
        <div className="text-right hidden sm:block">
          <div className="text-sm font-medium text-[var(--ink)]">{user}</div>
          {userSub && (
            <div className="font-mono text-[11px] text-[var(--ink-3)]">{userSub}</div>
          )}
        </div>
      )}
      {userName && <Avatar name={userName} />}
      {onLogout && (
        <button
          onClick={onLogout}
          className="flex items-center gap-1.5 h-9 px-3 rounded-[var(--radius-sm)] text-[var(--ink-2)] hover:bg-[var(--bg-sunk)] text-sm transition"
          title="Abmelden"
        >
          <Icon name="logout" size={18} />
          Abmelden
        </button>
      )}
    </div>
  </header>
);
