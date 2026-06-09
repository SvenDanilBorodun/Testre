#!/usr/bin/env python3
"""Build / inspect the EduBotics offline-bundle payload container ("EDBP1").

This is the WRITER side of the format that the C launcher (edb_payload.c) reads.
The two MUST stay in lockstep; the CI roundtrip test packs with this script and
extracts with the compiled C core to prove they agree (incl. a >4 GiB member).

Container format (all integers BIG-ENDIAN, no compression — members are
.tar.gz already):

    magic    : 5 bytes  = b"EDBP1"
    total    : uint64   = sum of every member's data size
    count    : uint32   = number of members
    then `count` members, each:
      name_len : uint16   = byte length of name
      name     : name_len bytes, UTF-8, '/'-separated RELATIVE path
      size     : uint64   = member data size
      data     : size bytes

Subcommands:
    pack   <src_dir> <out.dat>     frame the src_dir tree into out.dat (streamed)
    list   <dat>                   print "<size>\t<name>" per member + the total
    unpack <dat> <dest_dir>        reference extractor (mirror of edb_payload.c)

Names are validated on BOTH pack and unpack: no absolute paths, drive letters,
backslashes, or '.'/'..' components — a hostile container can never escape the
destination directory (defence-in-depth that mirrors the C reader).
"""
from __future__ import annotations

import os
import struct
import sys

MAGIC = b"EDBP1"
CHUNK = 1 << 20  # 1 MiB streaming buffer


def _name_is_safe(name: str) -> bool:
    if not name or name[0] in ("/", "\\"):
        return False
    for comp in name.split("/"):
        if comp in ("", ".", ".."):
            return False
        if "\\" in comp or ":" in comp:
            return False
        if any(ord(c) < 0x20 for c in comp):
            return False
    return True


def _iter_files(src: str):
    """Yield (relname, fullpath, size) deterministically ('/'-relative)."""
    members = []
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for fname in sorted(files):
            full = os.path.join(root, fname)
            if not os.path.isfile(full):  # skip symlinks/specials
                continue
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            members.append((rel, full, os.path.getsize(full)))
    return members


def pack(src: str, out_path: str) -> tuple[int, int]:
    members = _iter_files(src)
    for rel, _, _ in members:
        if not _name_is_safe(rel):
            sys.exit(f"refusing to pack unsafe member name: {rel!r}")
    total = sum(sz for _, _, sz in members)
    with open(out_path, "wb") as out:
        out.write(MAGIC)
        out.write(struct.pack(">Q", total))
        out.write(struct.pack(">I", len(members)))
        for rel, full, sz in members:
            nb = rel.encode("utf-8")
            if len(nb) > 0xFFFF:
                sys.exit(f"member name too long ({len(nb)} bytes): {rel}")
            out.write(struct.pack(">H", len(nb)))
            out.write(nb)
            out.write(struct.pack(">Q", sz))
            copied = 0
            with open(full, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    out.write(b)
                    copied += len(b)
            if copied != sz:
                sys.exit(f"size of {rel} changed during packing ({copied} != {sz})")
    return total, len(members)


def _read_exact(f, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError("payload truncated (incomplete download)")
    return b


def _read_header(f) -> tuple[int, int]:
    if _read_exact(f, len(MAGIC)) != MAGIC:
        sys.exit("not an EDBP1 payload container")
    (total,) = struct.unpack(">Q", _read_exact(f, 8))
    (count,) = struct.unpack(">I", _read_exact(f, 4))
    return total, count


def list_members(dat: str) -> None:
    with open(dat, "rb") as f:
        total, count = _read_header(f)
        for _ in range(count):
            (nl,) = struct.unpack(">H", _read_exact(f, 2))
            name = _read_exact(f, nl).decode("utf-8")
            (size,) = struct.unpack(">Q", _read_exact(f, 8))
            # skip the data without buffering it
            remaining = size
            while remaining:
                step = min(remaining, CHUNK)
                if len(f.read(step)) != step:
                    raise EOFError("payload truncated (incomplete download)")
                remaining -= step
            print(f"{size}\t{name}")
        print(f"# total={total} count={count}")


def unpack(dat: str, dest: str) -> None:
    with open(dat, "rb") as f:
        _total, count = _read_header(f)
        os.makedirs(dest, exist_ok=True)
        for _ in range(count):
            (nl,) = struct.unpack(">H", _read_exact(f, 2))
            name = _read_exact(f, nl).decode("utf-8")
            if not _name_is_safe(name):
                sys.exit(f"unsafe member name in payload: {name!r}")
            (size,) = struct.unpack(">Q", _read_exact(f, 8))
            out_path = os.path.join(dest, *name.split("/"))
            os.makedirs(os.path.dirname(out_path) or dest, exist_ok=True)
            remaining = size
            with open(out_path, "wb") as o:
                while remaining:
                    step = min(remaining, CHUNK)
                    b = f.read(step)
                    if not b:
                        raise EOFError("payload truncated (incomplete download)")
                    o.write(b)
                    remaining -= len(b)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "pack" and len(argv) == 4:
        total, count = pack(argv[2], argv[3])
        print(f"packed {count} members, {total} payload bytes -> {argv[3]}")
    elif cmd == "list" and len(argv) == 3:
        list_members(argv[2])
    elif cmd == "unpack" and len(argv) == 4:
        unpack(argv[2], argv[3])
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
