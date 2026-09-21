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
import { render, screen, waitFor } from '@testing-library/react';
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

const editorMock = vi.hoisted(() => ({ throws: false }));
vi.mock('../CodeEditor', () => ({
  __esModule: true,
  default: function MockCodeEditor({ path, value }) {
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
