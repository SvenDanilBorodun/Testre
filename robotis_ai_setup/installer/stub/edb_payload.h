/*
 * edb_payload.h — EduBotics offline-bundle payload container (reader API).
 *
 * WHY THIS EXISTS
 * ---------------
 * Windows refuses to *launch* any single .exe whose on-disk size exceeds
 * ~4 GiB (the PE image-size field is a 32-bit ULONG; confirmed by Microsoft,
 * Flexera, and 7-Zip's Igor Pavlov). That is the real cause of the old
 * 18.98 GB "EduBotics_Setup_Full.exe" failing to open ("this type can't be
 * opened here"). A 64-bit SFX stub does NOT lift the ceiling — it is on the
 * file the OS is asked to execute, not on the stub's bitness. The only
 * loader-legal shape for a multi-GB offline bundle is therefore a SMALL
 * launcher .exe (well under 4 GiB) that reads the multi-GB payload from a
 * SEPARATE file with 64-bit I/O. That payload file is this container.
 *
 * The launcher (main.c) ships next to "EduBotics_Setup_Full.dat" (this
 * container). It extracts the .dat back into the original sfx_root tree
 * (EduBotics_Setup.exe + images/<repo>.tar.gz(.sha256) + bundled_digests.json)
 * and runs the inner Inno installer from there — so Inno's {src} is the
 * extract dir, and installer/scripts/stage_bundle.ps1 / load_images.ps1 / the
 * .iss are byte-UNCHANGED.
 *
 * CONTAINER FORMAT ("EDBP1") — all integers BIG-ENDIAN, no compression
 * (the members are .tar.gz already; re-compressing would be pointless):
 *
 *   header:
 *     magic    : 5 bytes  = "EDBP1"
 *     total    : uint64   = sum of every member's data size (for the progress
 *                           bar AND for truncation detection)
 *     count    : uint32   = number of members
 *   then `count` members, each:
 *     name_len : uint16   = byte length of `name`
 *     name     : name_len bytes, UTF-8, '/'-separated RELATIVE path
 *     size     : uint64   = member data size in bytes
 *     data     : size bytes
 *   EOF.
 *
 * Extraction is purely SEQUENTIAL — the reader never seeks, so there is no
 * 32-bit file-offset hazard anywhere; only per-member SIZE is 64-bit, handled
 * by a chunked copy loop with a uint64 counter. A short read before `total`
 * bytes have been extracted means a truncated/incomplete download.
 *
 * NAME SAFETY: the reader rejects absolute paths, drive letters, backslashes,
 * empty names, and any ".." component, so a malformed/hostile .dat can never
 * write outside the destination directory.
 *
 * This core is PORTABLE C (C99, stdio only) so the CI roundtrip test compiles
 * it on the Linux runner and exercises the >4 GiB offset arithmetic against a
 * synthetic sparse payload — the only way to prove the 64-bit math without a
 * Windows rig. On Windows it converts UTF-8 paths to UTF-16 internally and
 * uses _wfopen/_wmkdir so German usernames in the extract path work.
 */
#ifndef EDB_PAYLOAD_H
#define EDB_PAYLOAD_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    EDB_OK = 0,
    EDB_ERR_OPEN,       /* cannot open the .dat for reading                */
    EDB_ERR_FORMAT,     /* bad magic / version — not an EDBP1 container     */
    EDB_ERR_TRUNCATED,  /* unexpected EOF — incomplete/partial download     */
    EDB_ERR_NAME,       /* unsafe member name (traversal / absolute path)   */
    EDB_ERR_WRITE,      /* cannot create/write an output file               */
    EDB_ERR_MKDIR,      /* cannot create an output subdirectory             */
    EDB_ERR_ABORT       /* progress callback asked to stop (user cancelled) */
} edb_result;

/*
 * Progress callback. `done` = bytes of member data extracted so far,
 * `total` = the header's total. Return 0 to continue, non-zero to abort
 * (extraction then returns EDB_ERR_ABORT). On any abort/error the in-progress
 * output file is removed; already-extracted members are the caller's to clean
 * up (the launcher removes its whole stage dir).
 */
typedef int (*edb_progress_cb)(uint64_t done, uint64_t total, void *user);

/*
 * Read just the header's `total` (sum of member sizes) from `dat_path`,
 * without extracting — used for the launcher's free-space precheck.
 * `dat_path` is UTF-8 on every platform.
 */
edb_result edb_read_total(const char *dat_path, uint64_t *out_total);

/*
 * Extract every member of `dat_path` into `dest_dir` (both UTF-8). Creates
 * subdirectories as needed. Calls `progress` (if non-NULL) after each chunk.
 */
edb_result edb_extract(const char *dat_path, const char *dest_dir,
                       edb_progress_cb progress, void *user);

/* Human-readable (English, maintainer-facing) name for a result code. */
const char *edb_strerror(edb_result r);

#ifdef __cplusplus
}
#endif

#endif /* EDB_PAYLOAD_H */
