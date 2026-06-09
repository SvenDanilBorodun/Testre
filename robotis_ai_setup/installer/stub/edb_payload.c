/*
 * edb_payload.c — EduBotics offline-bundle payload container (reader).
 * Portable C99 (stdio only). See edb_payload.h for the format + rationale.
 *
 * Extraction is purely SEQUENTIAL (no seeking), so the only 64-bit quantity
 * is the per-member size, handled by a chunked copy loop with a uint64
 * counter — there is no >4 GiB file-offset hazard anywhere. The CI roundtrip
 * test compiles THIS file natively and extracts a synthetic >4 GiB sparse
 * payload to prove the size arithmetic before any Windows rig sees it.
 *
 * On Windows, UTF-8 member/dest paths are converted to UTF-16 and opened with
 * _wfopen/_wmkdir using a \\?\ long-path prefix (German usernames + deep
 * extract dirs never hit MAX_PATH). On POSIX, paths are used as-is (UTF-8).
 */
#include "edb_payload.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#ifdef _WIN32
#  include <windows.h>
#  include <wchar.h>
#  include <direct.h>   /* _wmkdir */
#else
#  include <sys/stat.h> /* mkdir   */
#  include <sys/types.h>
#endif

#define EDB_MAGIC      "EDBP1"
#define EDB_MAGIC_LEN  5
#define EDB_COPY_BUF   (1u << 20)   /* 1 MiB streaming buffer */

/* ── low-level reads ──────────────────────────────────────────────────── */

/* Read exactly n bytes; returns 1 on success, 0 on short read (truncation). */
static int read_exact(FILE *f, void *buf, size_t n) {
    return fread(buf, 1, n, f) == n;
}

static int read_u16be(FILE *f, uint16_t *out) {
    unsigned char b[2];
    if (!read_exact(f, b, 2)) return 0;
    *out = (uint16_t)((b[0] << 8) | b[1]);
    return 1;
}

static int read_u32be(FILE *f, uint32_t *out) {
    unsigned char b[4];
    if (!read_exact(f, b, 4)) return 0;
    *out = ((uint32_t)b[0] << 24) | ((uint32_t)b[1] << 16) |
           ((uint32_t)b[2] << 8) | (uint32_t)b[3];
    return 1;
}

static int read_u64be(FILE *f, uint64_t *out) {
    unsigned char b[8];
    if (!read_exact(f, b, 8)) return 0;
    *out = ((uint64_t)b[0] << 56) | ((uint64_t)b[1] << 48) |
           ((uint64_t)b[2] << 40) | ((uint64_t)b[3] << 32) |
           ((uint64_t)b[4] << 24) | ((uint64_t)b[5] << 16) |
           ((uint64_t)b[6] << 8)  | (uint64_t)b[7];
    return 1;
}

/* ── platform path helpers ────────────────────────────────────────────── */

#ifdef _WIN32
/* UTF-8 -> heap UTF-16; caller frees. NULL on failure. */
static wchar_t *widen(const char *utf8) {
    int n = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, NULL, 0);
    if (n <= 0) return NULL;
    wchar_t *w = (wchar_t *)malloc((size_t)n * sizeof(wchar_t));
    if (!w) return NULL;
    if (MultiByteToWideChar(CP_UTF8, 0, utf8, -1, w, n) <= 0) { free(w); return NULL; }
    return w;
}

/* UTF-16 path, \\?\-prefixed when it is a drive-absolute path (X:\...), so we
 * sidestep MAX_PATH unconditionally. Caller frees. */
static wchar_t *win_long_path(const char *utf8) {
    wchar_t *w = widen(utf8);
    if (!w) return NULL;
    /* Already a UNC/extended path, or not drive-absolute -> use as-is. */
    int absolute = (w[0] && w[1] == L':' && (w[2] == L'\\' || w[2] == L'/'));
    if (!absolute || (w[0] == L'\\' && w[1] == L'\\')) return w;
    size_t len = wcslen(w);
    wchar_t *p = (wchar_t *)malloc((len + 5) * sizeof(wchar_t)); /* \\?\ + NUL */
    if (!p) { free(w); return NULL; }
    wcscpy(p, L"\\\\?\\");
    wcscat(p, w);
    free(w);
    /* \\?\ forbids forward slashes — normalise. */
    for (wchar_t *c = p + 4; *c; ++c) if (*c == L'/') *c = L'\\';
    return p;
}
#endif

static FILE *edb_fopen(const char *path_utf8, int write) {
#ifdef _WIN32
    wchar_t *wp = win_long_path(path_utf8);
    if (!wp) return NULL;
    FILE *f = _wfopen(wp, write ? L"wb" : L"rb");
    free(wp);
    return f;
#else
    return fopen(path_utf8, write ? "wb" : "rb");
#endif
}

/* Create one directory; success if it already exists. */
static int edb_mkdir_one(const char *path_utf8) {
#ifdef _WIN32
    wchar_t *wp = win_long_path(path_utf8);
    if (!wp) return 0;
    int rc = _wmkdir(wp);
    int ok = (rc == 0) || (errno == EEXIST);
    free(wp);
    return ok;
#else
    int rc = mkdir(path_utf8, 0755);
    return (rc == 0) || (errno == EEXIST);
#endif
}

/* Best-effort remove of a file (UTF-8 path). */
static void edb_remove(const char *path_utf8) {
#ifdef _WIN32
    wchar_t *wp = win_long_path(path_utf8);
    if (wp) { _wremove(wp); free(wp); }
#else
    remove(path_utf8);
#endif
}

/* ── name safety ──────────────────────────────────────────────────────── */

/* Reject absolute paths, drive letters, backslashes, '.'/'..' components,
 * empty components, and control bytes — a hostile .dat must never escape
 * dest_dir. `name` is the NUL-terminated UTF-8 relative path ('/'-separated). */
static int name_is_safe(const char *name) {
    if (!name || name[0] == '\0') return 0;
    if (name[0] == '/' || name[0] == '\\') return 0;   /* absolute */
    size_t comp_len = 0;
    for (const char *p = name; ; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '\0' || c == '/') {
            if (comp_len == 0) return 0;                /* empty comp / "//" */
            comp_len = 0;
            if (c == '\0') break;
            continue;
        }
        if (c == '\\' || c == ':') return 0;            /* sep / drive / ADS */
        if (c < 0x20) return 0;                         /* control byte */
        /* reject a "." or ".." component */
        if (c == '.') {
            const char *q = p;
            size_t dots = 0;
            while (*q == '.') { ++dots; ++q; }
            if ((*q == '/' || *q == '\0') && comp_len == 0 && (dots == 1 || dots == 2))
                return 0;
        }
        ++comp_len;
    }
    return 1;
}

/* ── path building / mkdir -p ─────────────────────────────────────────── */

/* Join dest + '/' + name into out (UTF-8). Returns 0 if it would overflow. */
static int join_path(char *out, size_t out_sz, const char *dest, const char *name) {
    size_t dl = strlen(dest);
    size_t nl = strlen(name);
    int need_sep = (dl > 0 && dest[dl - 1] != '/' && dest[dl - 1] != '\\');
    if (dl + (need_sep ? 1 : 0) + nl + 1 > out_sz) return 0;
    memcpy(out, dest, dl);
    size_t pos = dl;
    if (need_sep) out[pos++] = '/';
    memcpy(out + pos, name, nl + 1);  /* include NUL */
    return 1;
}

/* Create every parent directory of `full` (a UTF-8 path) that lies BELOW the
 * already-existing dest root. `base` is strlen(dest_dir): the scan starts there
 * so we never mkdir the drive root, a bare "C:", or pre-existing system
 * ancestors (C:\Users\...\AppData\...) — only the dest root (harmless EEXIST)
 * and the genuinely-new subdirs. Mirrors pack_payload.py (which makedirs only
 * the member's dirname). */
static int mkdir_parents(char *full, size_t base) {
    for (char *p = full + base; *p; ++p) {
        if (*p == '/' || *p == '\\') {
            char saved = *p;
            *p = '\0';
            int ok = edb_mkdir_one(full);
            *p = saved;
            if (!ok) return 0;
        }
    }
    return 1;
}

/* ── public API ───────────────────────────────────────────────────────── */

const char *edb_strerror(edb_result r) {
    switch (r) {
        case EDB_OK:          return "ok";
        case EDB_ERR_OPEN:    return "cannot open payload file";
        case EDB_ERR_FORMAT:  return "not an EDBP1 payload container";
        case EDB_ERR_TRUNCATED: return "payload truncated (incomplete download)";
        case EDB_ERR_NAME:    return "unsafe member name in payload";
        case EDB_ERR_WRITE:   return "cannot write extracted file";
        case EDB_ERR_MKDIR:   return "cannot create extract directory";
        case EDB_ERR_ABORT:   return "extraction aborted";
        default:              return "unknown error";
    }
}

static int read_and_check_header(FILE *f, uint64_t *out_total, uint32_t *out_count) {
    char magic[EDB_MAGIC_LEN];
    if (!read_exact(f, magic, EDB_MAGIC_LEN)) return 0;
    if (memcmp(magic, EDB_MAGIC, EDB_MAGIC_LEN) != 0) return -1; /* bad magic */
    if (!read_u64be(f, out_total)) return 0;
    if (!read_u32be(f, out_count)) return 0;
    return 1;
}

edb_result edb_read_total(const char *dat_path, uint64_t *out_total) {
    FILE *f = edb_fopen(dat_path, 0);
    if (!f) return EDB_ERR_OPEN;
    uint64_t total = 0;
    uint32_t count = 0;
    int hr = read_and_check_header(f, &total, &count);
    fclose(f);
    if (hr == -1) return EDB_ERR_FORMAT;
    if (hr != 1)  return EDB_ERR_TRUNCATED;
    if (out_total) *out_total = total;
    return EDB_OK;
}

edb_result edb_extract(const char *dat_path, const char *dest_dir,
                       edb_progress_cb progress, void *user) {
    FILE *in = edb_fopen(dat_path, 0);
    if (!in) return EDB_ERR_OPEN;

    edb_result rc = EDB_OK;
    char *name = NULL;
    char *full = NULL;
    unsigned char *buf = NULL;
    FILE *out = NULL;

    uint64_t total = 0, done = 0;
    uint32_t count = 0;
    int hr = read_and_check_header(in, &total, &count);
    if (hr == -1) { rc = EDB_ERR_FORMAT; goto done; }
    if (hr != 1)  { rc = EDB_ERR_TRUNCATED; goto done; }

    /* Ensure the destination root exists. */
    if (!edb_mkdir_one(dest_dir)) { rc = EDB_ERR_MKDIR; goto done; }

    buf = (unsigned char *)malloc(EDB_COPY_BUF);
    if (!buf) { rc = EDB_ERR_WRITE; goto done; }

    for (uint32_t i = 0; i < count; ++i) {
        uint16_t name_len = 0;
        if (!read_u16be(in, &name_len)) { rc = EDB_ERR_TRUNCATED; goto done; }

        name = (char *)malloc((size_t)name_len + 1);
        if (!name) { rc = EDB_ERR_WRITE; goto done; }
        if (name_len && !read_exact(in, name, name_len)) { rc = EDB_ERR_TRUNCATED; goto done; }
        name[name_len] = '\0';

        /* An embedded NUL would make the C reader (C-string) and the Python
         * reader (full name_len) disagree on the name — reject it. */
        if (memchr(name, '\0', name_len)) { rc = EDB_ERR_NAME; goto done; }
        if (!name_is_safe(name)) { rc = EDB_ERR_NAME; goto done; }

        uint64_t size = 0;
        if (!read_u64be(in, &size)) { rc = EDB_ERR_TRUNCATED; goto done; }

        size_t full_sz = strlen(dest_dir) + 1 + name_len + 1;
        full = (char *)malloc(full_sz);
        if (!full || !join_path(full, full_sz, dest_dir, name)) { rc = EDB_ERR_WRITE; goto done; }

        if (!mkdir_parents(full, strlen(dest_dir))) { rc = EDB_ERR_MKDIR; goto done; }

        out = edb_fopen(full, 1);
        if (!out) { rc = EDB_ERR_WRITE; goto done; }

        uint64_t remaining = size;
        while (remaining > 0) {
            size_t chunk = (remaining < (uint64_t)EDB_COPY_BUF)
                               ? (size_t)remaining : EDB_COPY_BUF;
            size_t got = fread(buf, 1, chunk, in);
            if (got == 0) { rc = EDB_ERR_TRUNCATED; goto done; }
            if (fwrite(buf, 1, got, out) != got) { rc = EDB_ERR_WRITE; goto done; }
            remaining -= got;
            done += got;
            if (progress && progress(done, total, user)) { rc = EDB_ERR_ABORT; goto done; }
        }

        if (fclose(out) != 0) { out = NULL; rc = EDB_ERR_WRITE; goto done; }
        out = NULL;
        free(full); full = NULL;
        free(name); name = NULL;
    }

done:
    if (out) {
        fclose(out);
        /* Drop the partially-written member on any error/abort so a failed
         * extract leaves no truncated file (the launcher also removes the whole
         * stage dir; this also keeps edb_extract_cli / the CI test clean). */
        if (rc != EDB_OK && full) edb_remove(full);
    }
    if (in)   fclose(in);
    free(buf);
    free(full);
    free(name);
    return rc;
}
