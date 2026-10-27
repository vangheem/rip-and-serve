#!/usr/bin/env python3
"""Copy a disc, repeating the last good piece where the drive cannot read.

A bad sector used to be retried one 2 KB block at a time, and the drive can
sit for 30 seconds on each one. A scratch should cost a moment of the movie,
not hours. On a failed read, skip a megabyte and fill it with the last good
piece, so the movie still opens.

An existing image is continued, not started over.
"""

import ctypes
import errno
import glob
import os
import sys
import time

SECTOR = 2048
UNIT = 6144
CHUNK = 85 * UNIT
SKIP = 170 * UNIT
READ_TIMEOUT_MS = 5000
SKIP_LIMIT = 2 * 1024 * 1024 * 1024
SG_IO = 0x2285
SG_DXFER_FROM_DEV = -3
ZEROS = b"\0" * SKIP


class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


assert ctypes.sizeof(SgIoHdr) == 88


def disc_size(fd):
    try:
        import fcntl
        import struct

        raw = fcntl.ioctl(fd, 0x80081272, b"\0" * 8)
        return struct.unpack("Q", raw)[0]
    except OSError:
        return 0


def find_drive(preferred):
    if preferred and os.path.exists(preferred):
        return preferred
    found = sorted(glob.glob("/dev/sr*"))
    return found[0] if found else ""


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


def write_zeros(out, count):
    while count:
        piece = min(count, len(ZEROS))
        out.write(ZEROS[:piece])
        count -= piece


def last_good_unit(offset, data, previous):
    end = offset + len(data)
    unit_end = end - (end % UNIT)
    unit_start = unit_end - UNIT
    if unit_start < offset or unit_end > end:
        return previous
    return data[unit_start - offset:unit_end - offset]


def write_fill(out, count, unit):
    """A hole of zeros will not decrypt. Repeat the last good piece instead."""
    if not unit:
        write_zeros(out, count)
        return
    left = count
    while left:
        piece = unit if left >= UNIT else unit[:left]
        out.write(piece)
        left -= len(piece)


class Reader:
    def __init__(self, path):
        self.path = path
        self.fd = None
        self.use_sg = os.environ.get("COPY_DISC_NO_SG") != "1"
        self.fast_fails = 0
        self._buf = ctypes.create_string_buffer(CHUNK)
        self._cdb = (ctypes.c_ubyte * 10)()
        self._sense = ctypes.create_string_buffer(64)
        self.open()

    def open(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = os.open(self.path, os.O_RDONLY)

    def reopen(self, preferred):
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(2)
            device = find_drive(preferred) or find_drive("")
            if not device:
                continue
            self.path = device
            try:
                self.open()
            except OSError:
                continue
            print(f"drive came back as {device}", flush=True)
            return
        raise OSError(errno.ENODEV, "drive did not come back")

    def read(self, offset, size):
        if not self.use_sg:
            return self._read_block(offset, size)
        started = time.time()
        try:
            data = self._read_sg(offset, size)
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.ENXIO):
                raise
            broken = exc.errno in (
                errno.ENOTTY,
                errno.EINVAL,
                errno.EFAULT,
                errno.EOPNOTSUPP,
                errno.ENOSYS,
            )
            if not broken and time.time() - started < 0.15:
                self.fast_fails += 1
                broken = self.fast_fails >= 4
            if broken:
                print("timed reads are not available; skipping a megabyte on each error", flush=True)
                self.use_sg = False
                return self._read_block(offset, size)
            raise
        self.fast_fails = 0
        return data

    def _read_sg(self, offset, size):
        import fcntl

        sectors = size // SECTOR
        lba = offset // SECTOR
        cdb = self._cdb
        cdb[0] = 0x28
        cdb[2] = (lba >> 24) & 0xFF
        cdb[3] = (lba >> 16) & 0xFF
        cdb[4] = (lba >> 8) & 0xFF
        cdb[5] = lba & 0xFF
        cdb[7] = (sectors >> 8) & 0xFF
        cdb[8] = sectors & 0xFF
        hdr = SgIoHdr()
        hdr.interface_id = ord("S")
        hdr.dxfer_direction = SG_DXFER_FROM_DEV
        hdr.cmd_len = 10
        hdr.mx_sb_len = 64
        hdr.dxfer_len = size
        hdr.dxferp = ctypes.addressof(self._buf)
        hdr.cmdp = ctypes.addressof(cdb)
        hdr.sbp = ctypes.addressof(self._sense)
        hdr.timeout = READ_TIMEOUT_MS
        raw = ctypes.create_string_buffer(ctypes.sizeof(hdr))
        ctypes.memmove(raw, ctypes.addressof(hdr), ctypes.sizeof(hdr))
        try:
            fcntl.ioctl(self.fd, SG_IO, raw)
        except OSError:
            raise
        result = SgIoHdr.from_buffer_copy(raw.raw)
        if result.masked_status == 0 and result.host_status == 0 and result.resid == 0:
            return self._buf.raw[:size]
        raise OSError(
            errno.EIO,
            f"read failed status={result.status} host={result.host_status} resid={result.resid}",
        )

    def _read_block(self, offset, size):
        os.lseek(self.fd, offset, os.SEEK_SET)
        data = os.read(self.fd, size)
        if len(data) != size:
            raise OSError(errno.EIO, "short read")
        return data

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def copy(src, dest):
    device = find_drive(src) or src
    if not device:
        print("no optical drive", flush=True)
        return 2
    offset = 0
    if os.path.exists(dest):
        offset = os.path.getsize(dest)
        offset -= offset % UNIT
    out = open(dest, "r+b" if offset else "wb")
    if offset:
        out.truncate(offset)
        out.seek(offset)
    reader = Reader(device)
    total = disc_size(reader.fd)
    skipped = 0
    last_unit = None
    reported = time.time()
    print(f"copying {device} ({total} bytes) to {dest}", flush=True)
    if offset:
        print(f"resuming at {offset}", flush=True)
    try:
        while total == 0 or offset < total:
            size = CHUNK
            if total:
                size = min(size, total - offset)
                size -= size % UNIT
            if size <= 0:
                break
            try:
                data = reader.read(offset, size)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.ENXIO):
                    reader.reopen(src)
                    continue
                jump = SKIP
                if total:
                    jump = min(jump, total - offset)
                jump -= jump % UNIT
                if jump <= 0:
                    break
                write_fill(out, jump, last_unit)
                print(f"skipped {jump} bytes at {offset}", flush=True)
                skipped += jump
                offset += jump
                if skipped > SKIP_LIMIT:
                    print(f"too much unreadable data ({skipped} bytes)", flush=True)
                    return 2
                continue
            last_unit = last_good_unit(offset, data, last_unit)
            out.write(data)
            offset += len(data)
            now = time.time()
            if now - reported >= 20:
                print(f"copied {offset} bytes, skipped {skipped}", flush=True)
                out.flush()
                reported = now
    finally:
        out.flush()
        out.close()
        reader.close()
    print(f"done bytes={offset} skipped={skipped}", flush=True)
    if offset < 1024 * 1024:
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
