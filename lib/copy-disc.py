#!/usr/bin/env python3
"""Copy a disc, writing zeros where the drive cannot read.

A scratch should cost a moment of the movie, not the rest of the file.
"""

import errno
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


def copy(src, dest):
    out = open(dest, "wb")
    offset = 0
    skipped = 0
    reported = time.time()
    fd = os.open(src, os.O_RDONLY)
    total = disc_size(fd)

    def reopen(at):
        nonlocal fd
        os.close(fd)
        fd = None
        for _ in range(8):
            time.sleep(2)
            try:
                fd = os.open(src, os.O_RDONLY)
                os.lseek(fd, at, os.SEEK_SET)
                print(f"drive came back at {at}", flush=True)
                return
            except OSError:
                continue
        raise OSError(errno.ENODEV, "drive did not come back")

    def skip_sector(at):
        nonlocal offset, skipped, fd
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
        os.close(fd)
    print(f"done bytes={offset} skipped={skipped}", flush=True)
    if skipped > SKIP_LIMIT:
        return 2
    return 0


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: copy-disc.py DEVICE DEST")
    raise SystemExit(copy(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
