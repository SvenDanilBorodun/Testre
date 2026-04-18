// Copyright 2025 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// Shared design-system primitives used by redesigned student pages.

import React from 'react';
import clsx from 'clsx';

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
        'inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 border rounded-full whitespace-nowrap',
        toneMap[tone] || toneMap.neutral,
        className
      )}
    >
      {dot && (
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'currentColor' }} />
      )}
      {children}
    </span>
  );
};

export const Btn = ({ variant = 'ghost', size = 'md', children, className, ...rest }) => {
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
      className={clsx(
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

export const Card = ({ title, subtitle, children, className, right, padded = true }) => (
  <section
    className={clsx(
      'bg-white border border-[var(--line)] rounded-[var(--radius-lg)] shadow-soft',
      className
    )}
  >
    {(title || right) && (
      <header className="flex items-center justify-between gap-3 px-5 pt-4 pb-3 border-b border-[var(--line)]">
        <div className="min-w-0">
          {title && (
            <h3 className="text-sm font-semibold text-[var(--ink)] tracking-tight">{title}</h3>
          )}
          {subtitle && <p className="text-xs text-[var(--ink-3)] mt-0.5">{subtitle}</p>}
        </div>
        {right}
      </header>
    )}
    <div className={padded ? 'p-5' : ''}>{children}</div>
  </section>
);

export const Stat = ({ label, value, tone, trend, className }) => (
  <div className={clsx('flex flex-col', className)}>
    <span className="text-[11px] font-semibold tracking-wide uppercase text-[var(--ink-3)]">
      {label}
    </span>
    <span
      className={clsx(
        'font-mono font-semibold text-[22px] leading-none mt-1.5',
        tone === 'success' && 'text-[color:var(--success)]',
        tone === 'danger' && 'text-[color:var(--danger)]',
        tone === 'accent' && 'text-[var(--accent-ink)]'
      )}
    >
      {value}
    </span>
    {trend && <span className="text-[11px] text-[var(--ink-3)] mt-1">{trend}</span>}
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

export const SectionHeader = ({ eyebrow, title, description, right, className }) => (
  <div className={clsx('flex items-start justify-between gap-6', className)}>
    <div className="min-w-0">
      {eyebrow && (
        <div className="text-xs font-mono text-[var(--ink-3)] uppercase tracking-wider">
          {eyebrow}
        </div>
      )}
      {title && (
        <h1 className="text-[28px] font-semibold tracking-tight mt-1 leading-[1.2] text-[var(--ink)]">
          {title}
        </h1>
      )}
      {description && (
        <p className="text-[15px] text-[var(--ink-3)] mt-2">{description}</p>
      )}
    </div>
    {right && <div className="shrink-0 pt-1">{right}</div>}
  </div>
);

export const Progress = ({ pct = 0, tone = 'accent', className }) => (
  <div
    className={clsx(
      'h-1.5 w-full bg-[var(--bg-sunk)] rounded-full overflow-hidden',
      className
    )}
  >
    <div
      className="h-full rounded-full transition-[width] duration-500"
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
