/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The „Methoden" column (owner decision C): the toolbox's groups, an entry
// that opens to its signatures, parameters, return value and example, the
// example filled with the student's own names, and „Einfügen" / drag through
// the code document's ONE checked insertion path — handled like the
// drawer's „Einfügen". A read-only editor and the two types offer neither.

import React from 'react';
import {
  render, screen, within, fireEvent, waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MethodenPanel from '../MethodenPanel';
import { METHOD_DOCS } from '../methodDocs';
import { methodGroups } from '../methodGroups';
import { CODE_DE, formatCode } from '../codeMessagesDe';
import { SNIPPET_MIME } from '../snippetMime';

const mockToast = vi.hoisted(() => {
  const t = { error: vi.fn(), success: vi.fn() };
  return Object.assign(vi.fn(), t);
});
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: mockToast,
  useToasterStore: () => ({ toasts: [] }),
}));

const SAMMLUNG = { places: ['Tischmitte', 'Hoch'], recordings: ['Tanz'], objects: ['wuerfel'] };

function mount(props = {}) {
  const assetDoc = { insertSnippet: vi.fn(async () => ({ count: 1, file: 'main.py', firstLine: 4, lastLine: 4 })) };
  const onClose = vi.fn();
  const utils = render(
    <MethodenPanel language="python" assetDoc={assetDoc} sammlung={SAMMLUNG} onClose={onClose} {...props} />,
  );
  return { ...utils, assetDoc: props.assetDoc === undefined ? assetDoc : props.assetDoc, onClose };
}

const entry = (re) => screen.getByRole('button', { name: re });
// The opened entry's detail element (its id is the button's aria-controls).
const detailOf = (button) => {
  const id = button.getAttribute('aria-controls');
  // eslint-disable-next-line testing-library/no-node-access
  return document.getElementById(id);
};

beforeEach(() => {
  mockToast.error.mockClear();
  mockToast.success.mockClear();
});

describe('the groups', () => {
  test('are the toolbox’s, each a labelled region with its methods, then „Anzeigen"', () => {
    mount();
    const panel = screen.getByRole('region', { name: CODE_DE.METHODS_TITLE });
    expect(within(panel).getByRole('heading', { level: 2, name: CODE_DE.METHODS_TITLE })).toBeInTheDocument();
    expect(within(panel).getByText(CODE_DE.METHODS_HELP)).toBeInTheDocument();
    const groups = methodGroups(METHOD_DOCS);
    const headings = within(panel).getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(groups.map((g) => g.label));
    expect(headings[headings.length - 1]).toBe(CODE_DE.METHODS_GROUP_SHOW);
    for (const m of METHOD_DOCS.methods) {
      expect(within(panel).getAllByText(m.signature_py, { exact: false }).length).toBeGreaterThan(0);
    }
  });
});

describe('an entry', () => {
  test('opens to its signature, doc, parameters with range and default, and the example', async () => {
    mount();
    const button = entry(/^robot\.replay\(name, speed=1\.0\)/);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'true');
    const detail = within(detailOf(button));
    expect(detail.getByText('robot.replay(name, speed=1.0)')).toBeInTheDocument();
    expect(detail.getByRole('heading', { name: CODE_DE.METHODS_PARAMS })).toBeInTheDocument();
    expect(detail.getByRole('heading', { name: CODE_DE.METHODS_EXAMPLE })).toBeInTheDocument();
    expect(detail.queryByRole('heading', { name: CODE_DE.METHODS_RETURNS })).toBeNull();
    const speed = METHOD_DOCS.byName.replay.params[1];
    expect(detail.getByText(speed.doc_de)).toBeInTheDocument();
    expect(detail.getByText(
      ['0.25 bis 3.0', formatCode(CODE_DE.METHODS_DEFAULT, '1.0'), CODE_DE.METHODS_OPTIONAL].join(' · '),
    )).toBeInTheDocument();
    // The example takes the student's newest Bewegung.
    expect(detail.getByText('robot.replay("Tanz")')).toBeInTheDocument();
    // One open at a time; a second click closes it.
    await userEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  test('a value row says what comes back', async () => {
    mount();
    const button = entry(/^robot\.sees\(obj\)/);
    await userEvent.click(button);
    const detail = within(detailOf(button));
    expect(detail.getByRole('heading', { name: CODE_DE.METHODS_RETURNS })).toBeInTheDocument();
    expect(detail.getByText(METHOD_DOCS.byName.sees.returns_de)).toBeInTheDocument();
    expect(detail.getByText('gesehen = robot.sees("wuerfel")')).toBeInTheDocument();
  });

  test('the example uses the first Ziel of the Sammlung', async () => {
    mount();
    const button = entry(/^robot\.move_to\(target\)/);
    await userEvent.click(button);
    expect(within(detailOf(button)).getByText('robot.move_to("Tischmitte")')).toBeInTheDocument();
  });

  test('Java: the Java spelling, every overload, the Java sentence', async () => {
    mount({ language: 'java' });
    const button = entry(/^Robot\.moveTo\(…\)/);
    expect(button.textContent).toContain(formatCode(CODE_DE.METHODS_VARIANTS, 1));
    expect(button.textContent).toContain(METHOD_DOCS.byName.move_to.doc_de_java);
    await userEvent.click(button);
    const detail = within(detailOf(button));
    for (const sig of METHOD_DOCS.byName.move_to.signatures_java) {
      expect(detail.getByText(sig)).toBeInTheDocument();
    }
    expect(detail.getByText('Robot.moveTo("Tischmitte");')).toBeInTheDocument();
    expect(screen.queryByText('robot.move_to(target)')).toBeNull();
  });
});

describe('„Einfügen"', () => {
  test('inserts the example through insertSnippet({kind: "call"}) and says where', async () => {
    const { assetDoc } = mount();
    const button = entry(/^robot\.move_to\(target\)/);
    await userEvent.click(button);
    await userEvent.click(within(detailOf(button)).getByRole('button', {
      name: formatCode(CODE_DE.METHODS_INSERT_ARIA, 'robot.move_to(target)'),
    }));
    expect(assetDoc.insertSnippet).toHaveBeenCalledWith({
      kind: 'call', method: 'move_to', line: 'robot.move_to("Tischmitte")',
    });
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith(formatCode(CODE_DE.INSERTED_AT, 'main.py', 4)));
  });

  test('a refused spot shows the document’s German reason; a failed load says so', async () => {
    const refusing = { insertSnippet: vi.fn(async () => ({ count: 0, error: CODE_DE.CLICK_FIRST_HINT })) };
    const { unmount } = mount({ assetDoc: refusing });
    let button = entry(/^robot\.home\(\)/);
    await userEvent.click(button);
    await userEvent.click(within(detailOf(button)).getByRole('button', { name: /^Einfügen: robot\.home\(\)$/ }));
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(CODE_DE.CLICK_FIRST_HINT));
    expect(mockToast.success).not.toHaveBeenCalled();
    unmount();

    vi.spyOn(console, 'error').mockImplementation(() => {});
    const failing = { insertSnippet: vi.fn(async () => { throw new Error('chunk'); }) };
    mount({ assetDoc: failing });
    button = entry(/^robot\.home\(\)/);
    await userEvent.click(button);
    await userEvent.click(within(detailOf(button)).getByRole('button', { name: /^Einfügen: / }));
    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(CODE_DE.SAMMLUNG_INSERT_FAILED));
  });

  test('the example row drags the same checked call', async () => {
    mount();
    const button = entry(/^robot\.wait\(seconds\)/);
    await userEvent.click(button);
    const row = screen.getByTestId('method-example-wait');
    expect(row).toHaveAttribute('draggable', 'true');
    const setData = vi.fn();
    fireEvent.dragStart(row, { dataTransfer: { setData, effectAllowed: 'none' } });
    expect(setData).toHaveBeenCalledWith(SNIPPET_MIME, JSON.stringify({ kind: 'call', method: 'wait', line: 'robot.wait(1.5)' }));
  });

  test('a read-only editor gets neither the button nor the drag', async () => {
    mount({ readOnly: true });
    const button = entry(/^robot\.home\(\)/);
    await userEvent.click(button);
    expect(within(detailOf(button)).queryByRole('button', { name: /^Einfügen/ })).toBeNull();
    expect(screen.getByTestId('method-example-home')).not.toHaveAttribute('draggable');
  });

  test('without an asset document there is no button, but the drag stays (the editor drops it)', async () => {
    mount({ assetDoc: null });
    const button = entry(/^robot\.home\(\)/);
    await userEvent.click(button);
    expect(within(detailOf(button)).queryByRole('button', { name: /^Einfügen/ })).toBeNull();
    expect(screen.getByTestId('method-example-home')).toHaveAttribute('draggable', 'true');
  });
});

describe('the two types', () => {
  test('are explanations with a tag, a multi-line example, and no „Einfügen"', async () => {
    mount();
    const button = entry(/^Greifziel/);
    expect(button.textContent).toContain(CODE_DE.METHODS_TYPE);
    await userEvent.click(button);
    const detail = within(detailOf(button));
    expect(detail.getByText(METHOD_DOCS.types[0].doc_de)).toBeInTheDocument();
    expect(detail.getByText((_, el) => el.tagName === 'PRE' && el.textContent === METHOD_DOCS.types[0].example_py))
      .toBeInTheDocument();
    expect(detail.queryByRole('button', { name: /^Einfügen/ })).toBeNull();
  });

  test('Java shows the Java spelling', () => {
    mount({ language: 'java' });
    expect(entry(/^Robot\.Greifziel/)).toBeInTheDocument();
    expect(entry(/^Greifobjekt/)).toBeInTheDocument();
  });
});

describe('closing', () => {
  test('the close button closes', async () => {
    const { onClose } = mount();
    await userEvent.click(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test('Esc closes the FLOATING panel only', () => {
    const { onClose, unmount } = mount();
    fireEvent.keyDown(entry(/^robot\.home\(\)/), { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    unmount();
    const floating = mount({ floating: true });
    fireEvent.keyDown(entry(/^robot\.home\(\)/), { key: 'Escape' });
    expect(floating.onClose).toHaveBeenCalledTimes(1);
  });
});
