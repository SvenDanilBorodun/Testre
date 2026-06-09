/*
 * main.c — EduBotics offline-bundle launcher (Windows, mingw-w64 cross-build).
 *
 * Shipped as "EduBotics_Setup_Full.exe" NEXT TO "EduBotics_Setup_Full.dat"
 * (the EDBP1 payload container — see edb_payload.h). The student downloads
 * BOTH into one folder and double-clicks the .exe. This launcher:
 *
 *   1. Single-instance guard (named mutex).
 *   2. Finds its sibling .dat (same basename, ".dat"), so the versioned
 *      copies (..._X.Y.Z.exe + ..._X.Y.Z.dat) self-pair too.
 *   3. Cleans STALE leftover extract dirs (older than 2 h — never one a
 *      concurrent install is still using).
 *   4. Reads the payload's total size; German free-space precheck.
 *   5. Extracts the payload into %LOCALAPPDATA%\EduBotics\stage-<pid>-<tick>
 *      (a persistent, user-writable, same-volume-as-the-bundle dir), showing a
 *      German progress window with a working Cancel.
 *   6. Launches the extracted EduBotics_Setup.exe FROM that dir (so Inno's
 *      {src} resolves there, beside images/ + bundled_digests.json — the
 *      stage_bundle.ps1 contract is preserved with ZERO .ps1/.iss changes),
 *      then EXITS. It deliberately does NOT wait: the inner Inno installer
 *      self-elevates (its first non-elevated instance would exit early, so a
 *      wait is unreliable), and because the stage dir is persistent (not a
 *      volatile %TEMP%), nothing is torn down out from under the installer.
 *
 * WHY A LAUNCHER AT ALL: Windows refuses to LOAD any single .exe > ~4 GiB
 * (PE image-size is a 32-bit field). The old one-file SFX (stub + appended
 * 16 GB archive) was ~19 GB and could not start ("this type can't be opened
 * here"). Keeping the big data in a SEPARATE .dat keeps this launched PE a few
 * MB — always loadable — while the payload can be any size.
 *
 * Rule §1: German for everything the student reads; English for code/comments.
 * Build: x86_64-w64-mingw32-gcc -O2 -municode -mwindows
 *          -finput-charset=UTF-8 -fwide-exec-charset=UTF-16LE
 *          -o EduBotics_Setup_Full.exe main.c edb_payload.c
 *          -lcomctl32 -lshell32 -luser32 -lkernel32
 */
#ifndef UNICODE
#  define UNICODE
#endif
#ifndef _UNICODE
#  define _UNICODE
#endif

#include <windows.h>
#include <shlobj.h>
#include <commctrl.h>
#include <strsafe.h>
#include <stdint.h>

#include "edb_payload.h"

#define APP_TITLE   L"EduBotics Setup"
#define DAT_BASENAME L"EduBotics_Setup_Full"  /* fallback if the .exe name is odd */
#define INNER_EXE   L"EduBotics_Setup.exe"
#define ID_CANCEL   1
#define WM_APP_PROGRESS (WM_APP + 1)
#define WM_APP_DONE     (WM_APP + 2)
#define STALE_MAX_SECONDS (2 * 60 * 60)      /* never delete a <2 h-old stage dir */
#define HEADROOM_BYTES    (512ull << 20)     /* free-space margin over payload size */

/* ── shared state (single instance, so module-static is safe) ─────────────── */
static volatile LONG       g_cancel = 0;
static volatile ULONGLONG  g_done   = 0;
static volatile ULONGLONG  g_total  = 1;
static volatile LONG       g_result = EDB_OK;
static HWND  g_hwnd  = NULL;
static HWND  g_bar   = NULL;
static HWND  g_label = NULL;
static char *g_dat_utf8   = NULL;
static char *g_stage_utf8 = NULL;

/* ── helpers ──────────────────────────────────────────────────────────────── */

static void show_error(const wchar_t *msg) {
    MessageBoxW(NULL, msg, APP_TITLE, MB_OK | MB_ICONERROR);
}

/* UTF-16 -> heap UTF-8; caller frees. NULL on failure. */
static char *to_utf8(const wchar_t *w) {
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (n <= 0) return NULL;
    char *s = (char *)malloc((size_t)n);
    if (!s) return NULL;
    if (WideCharToMultiByte(CP_UTF8, 0, w, -1, s, n, NULL, NULL) <= 0) { free(s); return NULL; }
    return s;
}

/* Recursively delete a directory tree (best-effort). */
static void remove_tree(const wchar_t *dir) {
    wchar_t pattern[MAX_PATH * 2];
    if (FAILED(StringCchPrintfW(pattern, MAX_PATH * 2, L"%ls\\*", dir))) return;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;
            wchar_t child[MAX_PATH * 2];
            if (FAILED(StringCchPrintfW(child, MAX_PATH * 2, L"%ls\\%ls", dir, fd.cFileName))) continue;
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                remove_tree(child);
            } else {
                SetFileAttributesW(child, FILE_ATTRIBUTE_NORMAL);
                DeleteFileW(child);
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    RemoveDirectoryW(dir);
}

/* Remove leftover stage-* dirs older than STALE_MAX_SECONDS, so a previous
 * install's extract dir is reclaimed WITHOUT ever touching one that a
 * concurrent install (this launcher exits before Inno finishes) is still using. */
static void clean_stale_stages(const wchar_t *edb_root) {
    wchar_t pattern[MAX_PATH * 2];
    if (FAILED(StringCchPrintfW(pattern, MAX_PATH * 2, L"%ls\\stage-*", edb_root))) return;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return;

    FILETIME nowft;
    GetSystemTimeAsFileTime(&nowft);
    ULONGLONG now = ((ULONGLONG)nowft.dwHighDateTime << 32) | nowft.dwLowDateTime;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;
        ULONGLONG mt = ((ULONGLONG)fd.ftLastWriteTime.dwHighDateTime << 32) | fd.ftLastWriteTime.dwLowDateTime;
        ULONGLONG age_sec = (now > mt) ? (now - mt) / 10000000ull : 0; /* 100 ns ticks */
        if (age_sec < STALE_MAX_SECONDS) continue;
        wchar_t full[MAX_PATH * 2];
        if (SUCCEEDED(StringCchPrintfW(full, MAX_PATH * 2, L"%ls\\%ls", edb_root, fd.cFileName)))
            remove_tree(full);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

/* ── extraction worker + progress window ──────────────────────────────────── */

static int progress_cb(uint64_t done, uint64_t total, void *user) {
    (void)user;
    g_done = done;
    g_total = total ? total : 1;
    static int last_pct = -1;
    int pct = (int)((done * 100) / (total ? total : 1));
    if (pct != last_pct) { last_pct = pct; PostMessageW(g_hwnd, WM_APP_PROGRESS, 0, 0); }
    return InterlockedCompareExchange(&g_cancel, 0, 0) ? 1 : 0;
}

static DWORD WINAPI extract_thread(LPVOID param) {
    (void)param;
    g_result = (LONG)edb_extract(g_dat_utf8, g_stage_utf8, progress_cb, NULL);
    PostMessageW(g_hwnd, WM_APP_DONE, 0, 0);
    return 0;
}

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
        case WM_APP_PROGRESS: {
            ULONGLONG d = g_done, t = g_total ? g_total : 1;
            SendMessageW(g_bar, PBM_SETPOS, (WPARAM)((d * 100) / t), 0);
            wchar_t buf[160];
            double dg = (double)d / (1024.0 * 1024.0 * 1024.0);
            double tg = (double)t / (1024.0 * 1024.0 * 1024.0);
            if (SUCCEEDED(StringCchPrintfW(buf, 160,
                    L"Installationsdaten werden entpackt … %.1f von %.1f GB", dg, tg)))
                SetWindowTextW(g_label, buf);
            return 0;
        }
        case WM_APP_DONE:
            /* Worker finished. The wWinMain pump exits on the thread HANDLE
             * signalling (robust even if this post is ever dropped); this case
             * just wakes the pump promptly. */
            return 0;
        case WM_COMMAND:
            if (LOWORD(w) == ID_CANCEL) {
                InterlockedExchange(&g_cancel, 1);
                EnableWindow((HWND)l, FALSE);
                SetWindowTextW(g_label, L"Wird abgebrochen …");
            }
            return 0;
        case WM_CLOSE:                         /* X button = cancel; let the worker unwind */
            InterlockedExchange(&g_cancel, 1);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

/* Build the progress window + controls; returns FALSE on failure. */
static BOOL build_window(HINSTANCE inst) {
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_PROGRESS_CLASS };
    InitCommonControlsEx(&icc);

    WNDCLASSW wc = {0};
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursorW(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"EduBoticsSetupFullWnd";
    if (!RegisterClassW(&wc)) return FALSE;

    int ww = 460, wh = 170;
    int sx = GetSystemMetrics(SM_CXSCREEN), sy = GetSystemMetrics(SM_CYSCREEN);
    g_hwnd = CreateWindowExW(0, wc.lpszClassName, APP_TITLE,
                             WS_CAPTION | WS_SYSMENU,
                             (sx - ww) / 2, (sy - wh) / 2, ww, wh,
                             NULL, NULL, inst, NULL);
    if (!g_hwnd) return FALSE;

    g_label = CreateWindowExW(0, L"STATIC",
                              L"Installationsdaten werden entpackt …",
                              WS_CHILD | WS_VISIBLE,
                              18, 18, ww - 36, 40, g_hwnd, NULL, inst, NULL);
    g_bar = CreateWindowExW(0, PROGRESS_CLASSW, NULL,
                            WS_CHILD | WS_VISIBLE | PBS_SMOOTH,
                            18, 64, ww - 36, 22, g_hwnd, NULL, inst, NULL);
    HWND btn = CreateWindowExW(0, L"BUTTON", L"Abbrechen",
                               WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
                               ww - 130, 100, 104, 28, g_hwnd, (HMENU)ID_CANCEL, inst, NULL);
    SendMessageW(g_bar, PBM_SETRANGE, 0, MAKELPARAM(0, 100));

    HFONT font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
    SendMessageW(g_label, WM_SETFONT, (WPARAM)font, TRUE);
    SendMessageW(btn, WM_SETFONT, (WPARAM)font, TRUE);

    ShowWindow(g_hwnd, SW_SHOW);
    UpdateWindow(g_hwnd);
    return TRUE;
}

/* ── entry point ──────────────────────────────────────────────────────────── */

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR cmd, int show) {
    (void)prev; (void)cmd; (void)show;

    /* 1. Single instance. Capture last-error IMMEDIATELY; if the mutex can't be
     *    created at all (NULL), fail safe — running unguarded would let a second
     *    concurrent install corrupt the shared %ProgramData%\EduBotics\bundle dir
     *    that stage_bundle.ps1 wipes + rewrites. */
    HANDLE mtx = CreateMutexW(NULL, FALSE, L"Local\\EduBoticsSetupFullLauncher");
    DWORD mtx_err = GetLastError();
    if (mtx == NULL) {
        show_error(L"Interner Fehler: Das Setup konnte nicht gestartet werden.");
        return 1;
    }
    if (mtx_err == ERROR_ALREADY_EXISTS) {
        MessageBoxW(NULL, L"EduBotics-Setup läuft bereits.", APP_TITLE, MB_OK | MB_ICONINFORMATION);
        return 0;
    }
    (void)mtx;  /* held for the process lifetime (released on exit) */

    /* 2. Locate self + the sibling .dat (same basename). */
    wchar_t exe_path[MAX_PATH];
    if (GetModuleFileNameW(NULL, exe_path, MAX_PATH) == 0) {
        show_error(L"Interner Fehler: Programmpfad konnte nicht ermittelt werden.");
        return 1;
    }
    wchar_t dir[MAX_PATH] = {0};
    wchar_t base[128];
    wchar_t *slash = wcsrchr(exe_path, L'\\');
    if (slash) {
        size_t dl = (size_t)(slash - exe_path);
        if (dl >= MAX_PATH) dl = MAX_PATH - 1;
        wcsncpy(dir, exe_path, dl);
        dir[dl] = L'\0';
        StringCchCopyW(base, 128, slash + 1);
    } else {
        StringCchCopyW(base, 128, exe_path);
    }
    size_t bl = wcslen(base);
    if (bl >= 4 && _wcsicmp(base + bl - 4, L".exe") == 0) base[bl - 4] = L'\0';
    if (base[0] == L'\0') StringCchCopyW(base, 128, DAT_BASENAME);

    wchar_t dat_path[MAX_PATH * 2];
    StringCchPrintfW(dat_path, MAX_PATH * 2, L"%ls\\%ls.dat", dir, base);
    if (GetFileAttributesW(dat_path) == INVALID_FILE_ATTRIBUTES) {
        show_error(L"Die Datei „EduBotics_Setup_Full.dat\" wurde nicht gefunden.\n\n"
                   L"Bitte laden Sie BEIDE Dateien (…Full.exe und …Full.dat) in denselben "
                   L"Ordner herunter und starten Sie dann EduBotics_Setup_Full.exe erneut.");
        return 1;
    }

    /* 3. %LOCALAPPDATA%\EduBotics + clean stale stage dirs. */
    wchar_t local_appdata[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, local_appdata))) {
        show_error(L"Interner Fehler: Benutzerordner konnte nicht ermittelt werden.");
        return 1;
    }
    wchar_t edb_root[MAX_PATH];
    StringCchPrintfW(edb_root, MAX_PATH, L"%ls\\EduBotics", local_appdata);
    CreateDirectoryW(edb_root, NULL);
    clean_stale_stages(edb_root);

    /* 4. Total payload size + free-space precheck. */
    g_dat_utf8 = to_utf8(dat_path);
    if (!g_dat_utf8) { show_error(L"Interner Fehler: Pfad-Umwandlung fehlgeschlagen."); return 1; }
    uint64_t total = 0;
    edb_result tr = edb_read_total(g_dat_utf8, &total);
    if (tr == EDB_ERR_TRUNCATED) {
        show_error(L"Die Datei „EduBotics_Setup_Full.dat\" ist unvollständig "
                   L"(Download abgebrochen?). Bitte erneut herunterladen.");
        return 1;
    }
    if (tr != EDB_OK) {
        show_error(L"Die Datei „EduBotics_Setup_Full.dat\" ist beschädigt oder ungültig. "
                   L"Bitte erneut herunterladen.");
        return 1;
    }
    ULARGE_INTEGER free_avail;
    if (GetDiskFreeSpaceExW(edb_root, &free_avail, NULL, NULL)) {
        uint64_t need = total + HEADROOM_BYTES;
        if (free_avail.QuadPart < need) {
            wchar_t msg[320];
            double need_gb = (double)need / (1024.0 * 1024.0 * 1024.0);
            double free_gb = (double)free_avail.QuadPart / (1024.0 * 1024.0 * 1024.0);
            StringCchPrintfW(msg, 320,
                L"Zu wenig Speicherplatz: ca. %.1f GB werden benötigt, aber nur %.1f GB sind frei.\n\n"
                L"Bitte geben Sie Speicherplatz frei und starten Sie die Installation erneut.",
                need_gb, free_gb);
            show_error(msg);
            return 1;
        }
    }

    /* 5. Fresh, unique, persistent stage dir on the LOCALAPPDATA volume. */
    wchar_t stage[MAX_PATH];
    StringCchPrintfW(stage, MAX_PATH, L"%ls\\stage-%lu-%lu",
                     edb_root, GetCurrentProcessId(), GetTickCount());
    if (!CreateDirectoryW(stage, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) {
        show_error(L"Die Installationsdaten konnten nicht vorbereitet werden (Schreibrechte?).");
        return 1;
    }
    g_stage_utf8 = to_utf8(stage);
    if (!g_stage_utf8) { show_error(L"Interner Fehler: Pfad-Umwandlung fehlgeschlagen."); remove_tree(stage); return 1; }
    g_total = total ? total : 1;

    /* 6. Progress window + extraction worker; pump until done/cancel. */
    if (!build_window(inst)) {
        show_error(L"Interner Fehler: Fortschrittsfenster konnte nicht erstellt werden.");
        remove_tree(stage);
        return 1;
    }
    HANDLE th = CreateThread(NULL, 0, extract_thread, NULL, 0, NULL);
    if (!th) {
        show_error(L"Interner Fehler: Entpack-Vorgang konnte nicht gestartet werden.");
        DestroyWindow(g_hwnd);
        remove_tree(stage);
        return 1;
    }
    /* Pump messages until the worker THREAD HANDLE signals (set when
     * extract_thread returns) — robust even if a WM_APP_DONE post is ever
     * dropped — while keeping the progress window + Cancel responsive. */
    for (;;) {
        DWORD w = MsgWaitForMultipleObjects(1, &th, FALSE, INFINITE, QS_ALLINPUT);
        if (w == WAIT_OBJECT_0 + 1) {
            MSG msg;
            while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        } else {
            break;  /* WAIT_OBJECT_0 (worker done) or WAIT_FAILED */
        }
    }
    WaitForSingleObject(th, INFINITE);
    CloseHandle(th);
    DestroyWindow(g_hwnd);

    /* 7. Handle the extraction result. */
    edb_result r = (edb_result)g_result;
    if (r == EDB_ERR_ABORT) { remove_tree(stage); return 0; }   /* user cancelled */
    if (r == EDB_ERR_TRUNCATED) {
        show_error(L"Die Datei „EduBotics_Setup_Full.dat\" ist unvollständig "
                   L"(Download abgebrochen?). Bitte erneut herunterladen.");
        remove_tree(stage);
        return 1;
    }
    if (r != EDB_OK) {
        show_error(L"Die Installationsdaten konnten nicht entpackt werden. "
                   L"Bitte Speicherplatz und Schreibrechte prüfen und erneut versuchen.");
        remove_tree(stage);
        return 1;
    }

    /* 8. Launch the inner Inno installer FROM the stage dir (Inno {src} = stage,
     *    beside images/ + bundled_digests.json) and exit — do NOT wait (the
     *    installer self-elevates; the stage dir is persistent so nothing is torn
     *    down out from under it). */
    wchar_t inner[MAX_PATH];
    StringCchPrintfW(inner, MAX_PATH, L"%ls\\%ls", stage, INNER_EXE);
    STARTUPINFOW si;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {0};
    if (!CreateProcessW(inner, NULL, NULL, NULL, FALSE, 0, NULL, stage, &si, &pi)) {
        show_error(L"Das Setup konnte nicht gestartet werden. "
                   L"Bitte EduBotics_Setup_Full.exe erneut ausführen.");
        remove_tree(stage);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
