// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// HeartbeatStatus as a „Sucher" readout (spec §6.2): a light by state (ok
// breathing / warn / rec), the status word as its own node, the latency; every
// state and string unchanged.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import HeartbeatStatus from '../../HeartbeatStatus';

vi.mock('../../../utils/piMode', () => ({
  usePiMode: () => ({ piMode: false, agentStatus: null }),
  PI_PORT_BLOCKED_HINT: 'Hinweis',
}));

function renderWith(status, last = Date.now(), props = {}) {
  const store = configureStore({
    reducer: { tasks: (s = { heartbeatStatus: status, lastHeartbeatTime: last }) => s },
  });
  return render(<Provider store={store}><HeartbeatStatus {...props} /></Provider>);
}

describe('HeartbeatStatus (readout)', () => {
  it.each([
    ['connected', 'Verbunden', 'su-led--ok', 'su-led--breathe'],
    ['timeout', 'Zeitüberschreitung', 'su-led--warn', 'su-led--none'],
    ['disconnected', 'Getrennt', 'su-led--rec', 'su-led--none'],
  ])('%s: light %s, word „%s" as its own node', (status, word, tone, pulse) => {
    const { container } = renderWith(status);
    expect(screen.getByText(word)).toBeInTheDocument();
    const root = container.firstChild;
    expect(root).toHaveClass('su-read', 'su-read--md', 'su-hb');
    // eslint-disable-next-line testing-library/no-node-access
    const led = root.querySelector('.su-led');
    expect(led).toHaveClass(tone, pulse);
    expect(led).toHaveAttribute('aria-hidden', 'true');
  });

  it('the latency while connected, „—" otherwise', () => {
    renderWith('connected', Date.now() - 5);
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('.su-hb__latency').textContent).toMatch(/^\d+ms$/);
  });

  it('„—" when not connected', () => {
    renderWith('disconnected', null);
    // eslint-disable-next-line testing-library/no-node-access
    expect(document.querySelector('.su-hb__latency')).toHaveTextContent('—');
  });

  it.each([['sm', 'su-read--sm'], ['small', 'su-read--sm'], ['md', 'su-read--md'], ['large', 'su-read--md']])(
    'size %s → %s', (size, cls) => {
      const { container } = renderWith('connected', Date.now(), { size });
      expect(container.firstChild).toHaveClass(cls);
    },
  );

  it('showLabel={false} hides the word; className passes through; dark is accepted', () => {
    const { container } = renderWith('connected', Date.now(), { showLabel: false, className: 'ml-2', dark: true });
    expect(screen.queryByText('Verbunden')).toBeNull();
    expect(container.firstChild).toHaveClass('ml-2');
  });
});
