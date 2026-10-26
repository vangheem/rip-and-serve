#!/usr/bin/env python3
"""Copy a disc, writing zeros where the drive cannot read.

A scratch should cost a moment of the movie, not the rest of the file.
If the drive falls off the USB bus, wait for it to come back and continue.
"""

import errno
import glob
import os
import sys
import time

SECTOR = 2048
BIG = 1024 * 1024
SKIP_LIMIT = 200 * 1024 * 1024


def disc_size(fd):
    try:
        import fcntl
        import struct
        raw = fcntl.ioctl(fd, 0x80081272, b"\0" * 8)
        return struct.unpack("Q", raw)[0]
    except OSError:
        return 0


def drive_ready(path):
    try:
        import fcntl
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        return fcntl.ioctl(fd, 0x5326, 0) == 4
    except OSError:
        return False
    finally:
        os.close(fd)


def find_drive(preferred):
    if preferred and drive_ready(preferred):
        return preferred
    for path in sorted(glob.glob("/dev/sr*")):
        if drive_ready(path):
            return path
    return ""


def hide_hash_tables(path):
    """Stop MakeMKV aborting when a scratch fails the disc hash check."""
    needle = b"ContentHash"
    replacement = b"ContentHaxx"
    changed = 0
    with open(path, "r+b") as handle:
        while True:
            start = handle.tell()
            block = handle.read(8 * 1024 * 1024)
            if not block:
                break
            updated = block
            offset = 0
            while True:
                found = updated.find(needle, offset)
                if found < 0:
                    break
                after = updated[found + len(needle):found + len(needle) + 7]
                if len(after) >= 7 and after[:3].isdigit() and after[3:7] == b".tbl":
                    updated = updated[:found] + replacement + updated[found + len(needle):]
                    changed += 1
                offset = found + len(needle)
            if updated != block:
                handle.seek(start)
                handle.write(updated)
                handle.seek(start + len(block))
    print(f"hid {changed} content hash tables", flush=True)
    return 0


def copy(src, dest):
    out = open(dest, "wb")
    offset = 0
    skipped = 0
    reported = time.time()
    device = find_drive(src) or src
    fd = os.open(device, os.O_RDONLY)
    total = disc_size(fd)
    print(f"copying {device} ({total} bytes) to {dest}", flush=True)

    def reopen(at):
        nonlocal fd, device
        try:
            os.close(fd)
        except OSError:
            pass
        fd = None
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(3)
            device = find_drive(src) or find_drive("")
            if not device:
                continue
            try:
                fd = os.open(device, os.O_RDONLY)
                os.lseek(fd, at, os.SEEK_SET)
                print(f"drive came back as {device} at {at}", flush=True)
                return
            except OSError:
                continue
        raise OSError(errno.ENODEV, "drive did not come back")

    def skip_sector(at):
        nonlocal offset, skipped
        out.write(b"\0" * SECTOR)
        skipped += SECTOR
        offset = at + SECTOR
        try:
            os.lseek(fd, offset, os.SEEK_SET)
        except OSError:
            reopen(offset)
        print(f"skipped unreadable sector at {at}", flush=True)

    try:
        while total == 0 or offset < total:
            try:
                data = os.read(fd, BIG)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.ENXIO):
                    reopen(offset)
                    continue
                if exc.errno not in (errno.EIO, errno.EAGAIN, errno.ENOMEDIUM):
                    raise
                try:
                    os.lseek(fd, offset, os.SEEK_SET)
                except OSError:
                    reopen(offset)
                window_end = offset + (min(BIG, total - offset) if total else BIG)
                while offset < window_end and (total == 0 or offset < total):
                    try:
                        block = os.read(fd, SECTOR)
                    except OSError as inner:
                        if inner.errno in (errno.ENODEV, errno.ENXIO):
                            reopen(offset)
                            continue
                        skip_sector(offset)
                        if skipped > SKIP_LIMIT:
                            print(f"too much unreadable data ({skipped} bytes)", flush=True)
                            return 2
                        continue
                    if not block:
                        total = offset
                        break
                    out.write(block)
                    offset += len(block)
                continue
            if not data:
                break
            out.write(data)
            offset += len(data)
            now = time.time()
            if now - reported >= 20:
                print(f"copied {offset} bytes, skipped {skipped}", flush=True)
                reported = now
    finally:
        out.close()
        if fd is not None:
            os.close(fd)
    print(f"done bytes={offset} skipped={skipped}", flush=True)
    if offset < 1024 * 1024:
        return 2
    if skipped > SKIP_LIMIT:
        return 2
    return hide_hash_tables(dest)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--hide-hashes":
        raise SystemExit(hide_hash_tables(sys.argv[2]))
    if len(sys.argv) != 3:
        raise SystemExit("usage: copy-disc.py DEVICE DEST")
    raise SystemExit(copy(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
