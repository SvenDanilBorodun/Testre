/*
 * edb_extract_cli.c — portable CLI wrapper around edb_extract().
 *
 * Compiled NATIVELY on the Linux CI runner (gcc edb_payload.c edb_extract_cli.c)
 * for the bundle-stub-roundtrip test, which packs a synthetic >4 GiB payload
 * with pack_payload.py and extracts it here to prove the 64-bit size arithmetic
 * agrees with the writer — the only way to exercise the >4 GiB path without a
 * Windows rig. NOT part of the shipped Windows launcher (that is main.c).
 *
 * Exit code: 0 on success, the edb_result code (>0) on failure, 64 on misuse.
 */
#include "edb_payload.h"

#include <stdio.h>

static int progress(uint64_t done, uint64_t total, void *user) {
    (void)user;
    static int last = -1;
    int pct = total ? (int)((done * 100) / total) : 100;
    if (pct != last) { fprintf(stderr, "\r%3d%%", pct); fflush(stderr); last = pct; }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s <payload.dat> <dest_dir>\n", argv[0]);
        return 64;
    }
    edb_result r = edb_extract(argv[1], argv[2], progress, NULL);
    fprintf(stderr, "\n");
    if (r != EDB_OK) {
        fprintf(stderr, "extract failed: %s (code %d)\n", edb_strerror(r), (int)r);
        return (int)r;
    }
    fprintf(stderr, "extract ok\n");
    return 0;
}
