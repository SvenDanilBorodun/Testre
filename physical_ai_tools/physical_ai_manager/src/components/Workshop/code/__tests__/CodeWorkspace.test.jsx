/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every rule CodeWorkspace owns, and it owns them alone: the §3.7 caps at the
// moment a file is CREATED, the entry file that may be neither renamed nor
// deleted, the duplicate refusal, the rename's key rewrite, and the
// `edubotics_code_last_file` memory. `WorkshopPage.codeLanguage.test.jsx`
// cannot stand in for any of it — it mocks this component out wholesale.
//
// The editor is mocked: it is `lazy(() => import('./CodeEditor'))`, and the
// real one pulls CodeMirror into a jsdom that has no layout. The mock renders
// the active file's content so the file-tree assertions can read it.

import React from 'react';
import {
  fireEvent, render, screen, waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import CodeWorkspace from '../CodeWorkspace';
import { CODE_DE, formatCode } from '../codeMessagesDe';
import { CODE_LIMITS, ENTRY_FILE } from '../codeProject';
import workshopReducer from '../../../../features/workshop/workshopSlice';

vi.mock('../../../../hooks/useRosServiceCaller', () => ({
  __esModule: true,
  useRosServiceCaller: () => ({ setWorkflowBreakpoints: vi.fn(() => Promise.resolve({})) }),
}));

const mockToast = vi.hoisted(() => {
  const t = { error: vi.fn(), success: vi.fn() };
  return Object.assign(vi.fn(), t);
});
vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: mockToast,
  useToasterStore: () => ({ toasts: [] }),
}));

const editorMock = vi.hoisted(() => ({ throws: false, props: null }));
vi.mock('../CodeEditor', () => ({
  __esModule: true,
  default: function MockCodeEditor(props) {
    const { path, value } = props;
    editorMock.props = props;
    // Stands in for the lazy chunk failing to arrive — a stale WebView2 cache
    // after an image update is the field case (the webview URL carries a
    // cache-busting `_v=<IMAGE_TAG>` for exactly this reason).
    if (editorMock.throws) throw new Error('Loading chunk CodeEditor failed');
    return (
      <div data-testid="code-editor" data-path={path}>
        <pre data-testid="code-editor-value">{value}</pre>
      </div>
    );
  },
}));

const ENTRY = ENTRY_FILE.python;
const BASE_FILES = Object.freeze({
  [ENTRY]: 'import robot\nrobot.home()\n',
  'hilfe.py': '# Hilfsfunktionen für „Würfel schieben"\n',
  'formen/kreis.py': 'RADIUS = 3\n',
});

/**
 * Render, wait for the lazy editor, and hand back the onFilesChange spy.
 *
 * The Provider is here because the component reads the debugger's own state
 * (`s.workshop.breakpoints`, `currentBlockId`) out of Redux — see
 * CodeWorkspace.debugger.test.jsx, which owns those assertions. Nothing in
 * THIS file depends on that state; a default store is enough.
 */
async function mount(files = BASE_FILES, over = {}) {
  const onFilesChange = vi.fn();
  const store = configureStore({ reducer: { workshop: workshopReducer } });
  const utils = render(
    <CodeWorkspace language="python" files={files} onFilesChange={onFilesChange} {...over} />,
    { wrapper: ({ children }) => <Provider store={store}>{children}</Provider> },
  );
  await screen.findByTestId('code-editor');
  return { onFilesChange, ...utils };
}

const fileButton = (path) => screen.getByRole('option', { name: new RegExp(`${path}$`) });
const activePath = () => screen.getByTestId('code-editor').getAttribute('data-path');

beforeEach(() => {
  mockToast.error.mockClear();
  editorMock.throws = false;
  try {
    window.localStorage.clear();
  } catch (_) { /* jsdom without storage — readLastFile already tolerates it */ }
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the file cap', () => {
  test('a new file is refused at MAX_CODE_FILES, in German, before it asks for a name', async () => {
    // The cap is judged on the files ALREADY there, so the refusal must come
    // before the prompt — a student who is told "too many files" only after
    // typing a name has been made to do work for nothing.
    const full = { [ENTRY]: '' };
    for (let i = 1; i < CODE_LIMITS.MAX_CODE_FILES; i += 1) full[`datei${i}.py`] = '';
    expect(Object.keys(full)).toHaveLength(CODE_LIMITS.MAX_CODE_FILES);
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('nochwas.py');

    const { onFilesChange } = await mount(full);
    await userEvent.click(screen.getByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) }));

    expect(mockToast.error).toHaveBeenCalledWith(
      formatCode(CODE_DE.ERR_TOO_MANY_FILES, CODE_LIMITS.MAX_CODE_FILES),
    );
    expect(prompt).not.toHaveBeenCalled();
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('one file below the cap the same click creates the file', async () => {
    const nearly = { [ENTRY]: '' };
    for (let i = 1; i < CODE_LIMITS.MAX_CODE_FILES - 1; i += 1) nearly[`datei${i}.py`] = '';
    vi.spyOn(window, 'prompt').mockReturnValue('nochwas.py');

    const { onFilesChange } = await mount(nearly);
    await userEvent.click(screen.getByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) }));

    expect(mockToast.error).not.toHaveBeenCalled();
    expect(onFilesChange).toHaveBeenCalledTimes(1);
    expect(onFilesChange.mock.calls[0][0]['nochwas.py']).toBe('');
  });
});

describe('a new file name is judged with the document validator', () => {
  test.each([
    ['../heimlich.py', (p) => formatCode(CODE_DE.ERR_BAD_PATH, p)],
    ['robot.py', (p) => formatCode(CODE_DE.ERR_RESERVED, p)],
    ['edubotics_hilfe.py', (p) => formatCode(CODE_DE.ERR_RESERVED, p)],
    ['Hilfe.java', (p) => formatCode(CODE_DE.ERR_WRONG_EXT, p, 'python')],
  ])('%s is refused in German and creates nothing', async (path, expected) => {
    vi.spyOn(window, 'prompt').mockReturnValue(path);
    const { onFilesChange } = await mount();

    await userEvent.click(screen.getByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) }));

    expect(mockToast.error).toHaveBeenCalledWith(expected(path));
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('a name the project already has is refused as such, not silently overwritten', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('  hilfe.py  ');
    const { onFilesChange } = await mount();

    await userEvent.click(screen.getByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) }));

    expect(mockToast.error).toHaveBeenCalledWith(formatCode(CODE_DE.ERR_FILE_EXISTS, 'hilfe.py'));
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('a cancelled prompt is a no-op — no refusal, no file', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue(null);
    const { onFilesChange } = await mount();

    await userEvent.click(screen.getByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) }));

    expect(mockToast.error).not.toHaveBeenCalled();
    expect(onFilesChange).not.toHaveBeenCalled();
  });
});

describe('the entry file', () => {
  // The German refusals (ERR_ENTRY_RENAME / ERR_ENTRY_DELETE) are the backstop
  // BEHIND the `disabled` attribute, and `disabled` is what a student meets:
  // React fires no onClick for a disabled button, so what is asserted here is
  // the protection that is actually reachable — the buttons are off, and a
  // click neither asks for a name nor changes the project.
  test('cannot be renamed or deleted while it is the open file', async () => {
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('anders.py');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onFilesChange } = await mount();

    expect(activePath()).toBe(ENTRY);
    const rename = screen.getByRole('button', { name: CODE_DE.FILE_RENAME });
    const del = screen.getByRole('button', { name: CODE_DE.FILE_DELETE });
    expect(rename).toBeDisabled();
    expect(del).toBeDisabled();

    await userEvent.click(rename);
    await userEvent.click(del);
    expect(prompt).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('both buttons come back once another file is open', async () => {
    await mount();
    await userEvent.click(fileButton('hilfe.py'));

    expect(screen.getByRole('button', { name: CODE_DE.FILE_RENAME })).toBeEnabled();
    expect(screen.getByRole('button', { name: CODE_DE.FILE_DELETE })).toBeEnabled();
  });

  test('is marked in the list and never offered in read-only mode', async () => {
    await mount(BASE_FILES, { readOnly: true });

    expect(fileButton(ENTRY)).toHaveAttribute('title', CODE_DE.FILE_ENTRY_TITLE);
    expect(screen.queryByRole('button', { name: new RegExp(CODE_DE.FILE_NEW) })).toBeNull();
    expect(screen.queryByRole('button', { name: CODE_DE.FILE_RENAME })).toBeNull();
    expect(screen.queryByRole('button', { name: CODE_DE.FILE_DELETE })).toBeNull();
  });
});

describe('renaming', () => {
  test('rewrites exactly the one key and leaves every other file untouched', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('werkzeuge.py');
    const { onFilesChange } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_RENAME }));

    expect(onFilesChange).toHaveBeenCalledTimes(1);
    const next = onFilesChange.mock.calls[0][0];
    expect(Object.keys(next).sort()).toEqual(
      [ENTRY, 'formen/kreis.py', 'werkzeuge.py'].sort(),
    );
    // The CONTENT rides along — a rename that dropped it would lose the file.
    expect(next['werkzeuge.py']).toBe(BASE_FILES['hilfe.py']);
    expect(next[ENTRY]).toBe(BASE_FILES[ENTRY]);
    expect(next['formen/kreis.py']).toBe(BASE_FILES['formen/kreis.py']);
  });

  test('onto an existing name is refused and rewrites nothing', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('formen/kreis.py');
    const { onFilesChange } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_RENAME }));

    expect(mockToast.error).toHaveBeenCalledWith(
      formatCode(CODE_DE.ERR_FILE_EXISTS, 'formen/kreis.py'),
    );
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('to an invalid name is refused with the document validator’s sentence', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('robot.py');
    const { onFilesChange } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_RENAME }));

    expect(mockToast.error).toHaveBeenCalledWith(formatCode(CODE_DE.ERR_RESERVED, 'robot.py'));
    expect(onFilesChange).not.toHaveBeenCalled();
  });

  test('to the same name changes nothing at all', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('hilfe.py');
    const { onFilesChange } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_RENAME }));

    expect(mockToast.error).not.toHaveBeenCalled();
    expect(onFilesChange).not.toHaveBeenCalled();
  });
});

describe('deleting', () => {
  test('asks first, drops exactly that file and falls back to the entry file', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onFilesChange, rerender } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_DELETE }));

    expect(confirm).toHaveBeenCalledWith(formatCode(CODE_DE.FILE_DELETE_CONFIRM, 'hilfe.py'));
    const next = onFilesChange.mock.calls[0][0];
    expect(Object.keys(next).sort()).toEqual([ENTRY, 'formen/kreis.py'].sort());
    // The page owns `files`, so the fallback is only observable once it feeds
    // the shortened project back in — which is what it does.
    rerender(
      <CodeWorkspace language="python" files={next} onFilesChange={onFilesChange} />,
    );
    await waitFor(() => expect(activePath()).toBe(ENTRY));
  });

  test('a declined confirmation deletes nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { onFilesChange } = await mount();
    await userEvent.click(fileButton('hilfe.py'));

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.FILE_DELETE }));

    expect(onFilesChange).not.toHaveBeenCalled();
  });
});

describe('an editor that does not arrive', () => {
  test('is a German sentence, not a white screen, and the file tree stays', async () => {
    // The editor is a lazy chunk. A WebView2 whose cache survived an image
    // update asks for a chunk that is no longer served, and without a boundary
    // the throw takes the whole Roboter-Studio page with it.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    editorMock.throws = true;

    const store = configureStore({ reducer: { workshop: workshopReducer } });
    render(
      <Provider store={store}>
        <CodeWorkspace language="python" files={BASE_FILES} onFilesChange={vi.fn()} />
      </Provider>,
    );

    expect(await screen.findByText(CODE_DE.EDITOR_FAILED)).toBeInTheDocument();
    expect(screen.queryByTestId('code-editor')).toBeNull();
    expect(fileButton('hilfe.py')).toBeInTheDocument();
    spy.mockRestore();
  });
});

describe('the file last open', () => {
  test('is remembered under edubotics_code_last_file and reopened next mount', async () => {
    const first = await mount();
    await userEvent.click(fileButton('formen/kreis.py'));

    expect(window.localStorage.getItem('edubotics_code_last_file')).toBe('formen/kreis.py');
    expect(screen.getByTestId('code-editor-value')).toHaveTextContent('RADIUS = 3');

    first.unmount();
    const second = await mount();
    expect(activePath()).toBe('formen/kreis.py');
    expect(second.onFilesChange).not.toHaveBeenCalled();
  });

  test('a remembered file this project does not have falls back to the entry file', async () => {
    window.localStorage.setItem('edubotics_code_last_file', 'einer/anderen/datei.py');

    await mount();

    expect(activePath()).toBe(ENTRY);
  });
});

// ── The UX round: the Methoden column and what the page hands through ──────

describe('the Methoden column', () => {
  const panel = () => screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });

  test('is open by default, beside the editor; its close button turns it into the rail and back', async () => {
    await mount();
    expect(await panel()).toBeInTheDocument();
    expect(screen.getByTestId('code-methods')).toHaveAttribute('data-floating', 'false');
    const editorNode = screen.getByTestId('code-editor');

    await userEvent.click(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }));
    expect(screen.queryByRole('region', { name: CODE_DE.METHODS_TITLE })).toBeNull();
    const rail = screen.getByRole('button', { name: CODE_DE.METHODS_OPEN });
    expect(rail).toHaveTextContent(CODE_DE.METHODS_TITLE);
    expect(rail).toHaveFocus();
    // The editor stays the same node — it is never remounted.
    expect(screen.getByTestId('code-editor')).toBe(editorNode);

    await userEvent.click(rail);
    expect(await panel()).toBeInTheDocument();
    expect(screen.getByTestId('code-editor')).toBe(editorNode);
  });

  test('in a code area too narrow for both it floats, and Esc closes it', async () => {
    // The widths a narrow window measures: 600 px of code area, a 176-px file
    // list — below 176 + 320 + 220 + 16.
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rectOf() {
      let width = 0;
      if (this.dataset && this.dataset.testid === 'code-workspace') width = 600;
      else if (this.tagName === 'ASIDE' && this.dataset.testid !== 'code-methods') width = 176;
      return {
        width, height: 400, top: 0, left: 0, right: width, bottom: 400, x: 0, y: 0,
      };
    });
    await mount();
    await panel();
    expect(screen.getByTestId('code-methods')).toHaveAttribute('data-floating', 'true');
    const editorNode = screen.getByTestId('code-editor');
    fireEvent.keyDown(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }), { key: 'Escape' });
    expect(screen.queryByRole('region', { name: CODE_DE.METHODS_TITLE })).toBeNull();
    expect(screen.getByRole('button', { name: CODE_DE.METHODS_OPEN })).toHaveFocus();
    expect(screen.getByTestId('code-editor')).toBe(editorNode);
    rect.mockRestore();
  });

  test('docked, Esc does not close it (it is part of the page, not a layer)', async () => {
    await mount();
    await panel();
    fireEvent.keyDown(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }), { key: 'Escape' });
    expect(screen.getByRole('region', { name: CODE_DE.METHODS_TITLE })).toBeInTheDocument();
  });

  test('a column that fails to load says so and leaves the editor and the files', async () => {
    vi.resetModules();
    vi.doMock('../MethodenPanel', () => { throw new Error('Loading chunk MethodenPanel failed'); });
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { default: Fresh } = await import('../CodeWorkspace');
    const store = configureStore({ reducer: { workshop: workshopReducer } });
    render(
      <Provider store={store}>
        <Fresh language="python" files={BASE_FILES} onFilesChange={vi.fn()} />
      </Provider>,
    );
    expect(await screen.findByText(CODE_DE.METHODS_FAILED)).toBeInTheDocument();
    expect(screen.getByTestId('code-editor')).toBeInTheDocument();
    expect(fileButton('hilfe.py')).toBeInTheDocument();
    spy.mockRestore();
    vi.doUnmock('../MethodenPanel');
    vi.resetModules();
  });
});

describe('what the page hands through (K-1, R0-1, m-3)', () => {
  test('onSave and commandsRef reach the editor; a read-only editor gets no onSave', async () => {
    const onSave = vi.fn();
    const commandsRef = { current: null };
    const { unmount } = await mount(BASE_FILES, { onSave, commandsRef });
    expect(editorMock.props.onSave).toBe(onSave);
    expect(editorMock.props.commandsRef).toBe(commandsRef);
    unmount();
    await mount(BASE_FILES, { onSave, commandsRef, readOnly: true });
    expect(editorMock.props.onSave).toBeNull();
  });

  test('a page reveal carrying flash reaches the editor with it', async () => {
    const { rerender } = await mount();
    const store = configureStore({ reducer: { workshop: workshopReducer } });
    rerender(
      <Provider store={store}>
        <CodeWorkspace
          language="python"
          files={BASE_FILES}
          onFilesChange={vi.fn()}
          revealRequest={{ file: 'hilfe.py', line: 1, nonce: 7, flash: { from: 1, to: 1 } }}
        />
      </Provider>,
    );
    await waitFor(() => expect(editorMock.props.revealRequest).toMatchObject({ line: 1, flash: { from: 1, to: 1 } }));
    expect(activePath()).toBe('hilfe.py');
  });

  test('sidebarWidthRef stays null at a measured width of 0 and takes a positive one', async () => {
    const zero = { current: null };
    const first = await mount(BASE_FILES, { sidebarWidthRef: zero });
    expect(zero.current).toBeNull();
    first.unmount();

    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rectOf() {
      const width = this.tagName === 'ASIDE' && this.dataset.testid !== 'code-methods' ? 203.6 : 0;
      return {
        width, height: 400, top: 0, left: 0, right: width, bottom: 400, x: 0, y: 0,
      };
    });
    const measured = { current: null };
    await mount(BASE_FILES, { sidebarWidthRef: measured });
    expect(measured.current).toBe(204);
    rect.mockRestore();
  });
});

// ── The code panes' dividers and what this PC remembers (I2-int) ──────────

describe('the code panes are resizable and remembered on this PC', () => {
  const sidebarHandle = () => screen.getByRole('separator', { name: CODE_DE.CODE_SIDEBAR_RESIZE });
  const methodsHandle = () => screen.queryByRole('separator', { name: CODE_DE.METHODS_RESIZE });

  test('a divider stands between the file list and the editor, another before the docked column', async () => {
    await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    const handle = sidebarHandle();
    // eslint-disable-next-line testing-library/no-node-access
    expect(handle.previousElementSibling.tagName).toBe('ASIDE');
    // eslint-disable-next-line testing-library/no-node-access
    expect(handle.nextElementSibling).toContainElement(screen.getByTestId('code-editor'));
    // eslint-disable-next-line testing-library/no-node-access
    expect(methodsHandle().nextElementSibling).toBe(screen.getByTestId('code-methods'));
    expect(handle).toHaveAttribute('aria-valuenow', '176');
    expect(methodsHandle()).toHaveAttribute('aria-valuenow', '300');
  });

  test('a width the student sets is stored, and the next mount takes it', async () => {
    const first = await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    fireEvent.keyDown(sidebarHandle(), { key: 'ArrowRight' });
    fireEvent.keyDown(methodsHandle(), { key: 'ArrowLeft', shiftKey: true });
    expect(window.localStorage.getItem('edubotics_code_sidebar_width')).toBe('184');
    expect(window.localStorage.getItem('edubotics_code_methods_width')).toBe('332');
    first.unmount();

    await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    expect(sidebarHandle()).toHaveAttribute('aria-valuenow', '184');
    expect(methodsHandle()).toHaveAttribute('aria-valuenow', '332');
  });

  test('a drag of the dividers never remounts the editor', async () => {
    await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    const editorNode = screen.getByTestId('code-editor');
    fireEvent.keyDown(sidebarHandle(), { key: 'End' });
    fireEvent.doubleClick(methodsHandle());
    expect(screen.getByTestId('code-editor')).toBe(editorNode);
  });

  test('closing and opening the column is stored; a closed column stays closed next time', async () => {
    const first = await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    await userEvent.click(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }));
    expect(window.localStorage.getItem('edubotics_code_methods_open')).toBe('0');
    expect(methodsHandle()).toBeNull();
    first.unmount();

    await mount();
    const rail = screen.getByRole('button', { name: CODE_DE.METHODS_OPEN });
    expect(screen.queryByRole('region', { name: CODE_DE.METHODS_TITLE })).toBeNull();
    await userEvent.click(rail);
    expect(await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE })).toBeInTheDocument();
    expect(window.localStorage.getItem('edubotics_code_methods_open')).toBe('1');
  });

  // Verification round 1, F5d: the two dividers share one budget, so the
  // editor never gets less than 320 px — each bound counts the other pane (or
  // the rail) and both handles at their width right now, and a change on one
  // side re-clamps the other's committed width.
  describe('the editor keeps 320 px between the two dividers', () => {
    // A laid-out row: the panes take the widths their CSS variables say (the
    // handles 8 px, the rail 40), the editor the rest.
    function layOut(rootW) {
      // eslint-disable-next-line testing-library/no-node-access
      const root = () => document.querySelector('[data-testid="code-workspace"]');
      const varPx = (name, fallback) => {
        const v = root() ? root().style.getPropertyValue(name) : '';
        return v ? parseFloat(v) : fallback;
      };
      return vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rectOf() {
        const id = this.dataset ? this.dataset.testid : undefined;
        let width = 0;
        if (id === 'code-workspace') width = rootW;
        else if (this.tagName === 'ASIDE' && id !== 'code-methods') width = varPx('--code-sidebar-w', 176);
        else if (id === 'code-methods') width = this.dataset.floating === 'true' ? 300 : varPx('--code-methods-w', 300);
        else if (id === 'code-methods-rail') width = 40;
        else if (this.getAttribute && this.getAttribute('role') === 'separator') width = 8;
        return {
          width, height: 400, top: 0, left: 0, right: width, bottom: 400, x: 0, y: 0,
        };
      });
    }
    const now = (handle) => Number(handle.getAttribute('aria-valuenow'));
    const editorWidth = (rootW) => {
      const methods = methodsHandle() ? 8 + now(methodsHandle()) : 40;
      return rootW - now(sidebarHandle()) - 8 - methods;
    };

    test('widening the file list stops where the docked column leaves the editor 320 px', async () => {
      const rect = layOut(900);
      await mount();
      await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
      expect(screen.getByTestId('code-methods')).toHaveAttribute('data-floating', 'false');
      fireEvent.keyDown(sidebarHandle(), { key: 'End' });
      expect(now(sidebarHandle())).toBe(900 - 320 - 8 - 8 - 300);
      expect(editorWidth(900)).toBe(320);
      rect.mockRestore();
    });

    test('widening the column stops where the file list and both handles leave the editor 320 px', async () => {
      window.localStorage.setItem('edubotics_code_sidebar_width', '360');
      const rect = layOut(1100);
      await mount();
      await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
      fireEvent.keyDown(methodsHandle(), { key: 'End' });
      expect(now(methodsHandle())).toBe(1100 - 320 - 360 - 8 - 8);
      expect(editorWidth(1100)).toBe(320);
      rect.mockRestore();
    });

    test('room one side gives back returns to the other side’s committed width', async () => {
      window.localStorage.setItem('edubotics_code_sidebar_width', '360');
      window.localStorage.setItem('edubotics_code_methods_width', '520');
      const rect = layOut(1100);
      await mount();
      await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
      // Squeezed at first: 1100 − 320 − 360 − 16.
      expect(now(methodsHandle())).toBe(404);
      expect(editorWidth(1100)).toBe(320);
      // The file list narrows: the column grows back towards its 520 px.
      fireEvent.keyDown(sidebarHandle(), { key: 'Home' });
      expect(now(sidebarHandle())).toBe(140);
      expect(now(methodsHandle())).toBe(520);
      expect(editorWidth(1100)).toBeGreaterThanOrEqual(320);
      // Nothing was stored for the column by the re-clamp.
      expect(window.localStorage.getItem('edubotics_code_methods_width')).toBe('520');
      rect.mockRestore();
    });

    test('closing the column gives a squeezed file list its committed width back', async () => {
      window.localStorage.setItem('edubotics_code_sidebar_width', '360');
      const rect = layOut(950);
      await mount();
      await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
      // 950 − 320 − 8 − 8 − 300.
      expect(now(sidebarHandle())).toBe(314);
      expect(editorWidth(950)).toBe(320);
      await userEvent.click(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }));
      expect(now(sidebarHandle())).toBe(360);
      expect(editorWidth(950)).toBeGreaterThanOrEqual(320);
      expect(window.localStorage.getItem('edubotics_code_sidebar_width')).toBe('360');
      rect.mockRestore();
    });
  });

  test('a floating column has no width divider', async () => {
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function rectOf() {
      let width = 0;
      if (this.dataset && this.dataset.testid === 'code-workspace') width = 600;
      else if (this.tagName === 'ASIDE' && this.dataset.testid !== 'code-methods') width = 176;
      return {
        width, height: 400, top: 0, left: 0, right: width, bottom: 400, x: 0, y: 0,
      };
    });
    await mount();
    await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    expect(screen.getByTestId('code-methods')).toHaveAttribute('data-floating', 'true');
    expect(methodsHandle()).toBeNull();
    expect(sidebarHandle()).toBeInTheDocument();
    rect.mockRestore();
  });
});

describe('the methods icon', () => {
  test('marks the rail and the column’s header', async () => {
    await mount();
    const panel = await screen.findByRole('region', { name: CODE_DE.METHODS_TITLE });
    // eslint-disable-next-line testing-library/no-node-access
    expect(panel.querySelector('header svg[data-icon="methods"]')).not.toBeNull();
    await userEvent.click(screen.getByRole('button', { name: CODE_DE.METHODS_CLOSE }));
    // eslint-disable-next-line testing-library/no-node-access
    expect(screen.getByRole('button', { name: CODE_DE.METHODS_OPEN }).querySelector('svg[data-icon="methods"]'))
      .not.toBeNull();
  });
});
