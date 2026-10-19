"""Bounded, paged random access to a file (or bytes) for the metadata parsers.

Every read is bounds-checked; a malformed file raises :class:`ParseError`, never an
``IndexError`` or ``struct.error``. Reads go through a small page cache because the
parsers make many tiny reads (IFD entries, box headers) that would be slow over USB.
"""

from __future__ import annotations

import io
import os
import struct
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

PAGE_SIZE = 64 * 1024
MAX_PAGES = 16
MAX_READ = 16 * 1024 * 1024     # no single parser read may allocate more than this

_INT_SIZES = {1: "B", 2: "H", 4: "I", 8: "Q"}


class ParseError(ValueError):
    """The data is malformed or truncated. Parsers treat it as "no metadata"."""


class _Pager:
    """Reads a file through an LRU cache of fixed-size pages."""

    def __init__(self, fh: BinaryIO, size: int) -> None:
        self._fh = fh
        self.size = size
        self._pages: OrderedDict[int, bytes] = OrderedDict()

    def read(self, off: int, n: int) -> bytes:
        if n == 0:
            return b""
        if n > 4 * PAGE_SIZE:
            self._fh.seek(off)
            data = self._fh.read(n)
        else:
            first, last = off // PAGE_SIZE, (off + n - 1) // PAGE_SIZE
            joined = b"".join(self._page(i) for i in range(first, last + 1))
            start = off - first * PAGE_SIZE
            data = joined[start:start + n]
        if len(data) != n:
            raise ParseError(f"short read at {off} (file changed or truncated)")
        return data

    def _page(self, index: int) -> bytes:
        page = self._pages.get(index)
        if page is not None:
            self._pages.move_to_end(index)
            return page
        self._fh.seek(index * PAGE_SIZE)
        page = self._fh.read(PAGE_SIZE)
        self._pages[index] = page
        if len(self._pages) > MAX_PAGES:
            self._pages.popitem(last=False)
        return page


class RandomAccess:
    """A bounds-checked view of ``size`` bytes starting at ``base`` of a paged source."""

    def __init__(self, pager: _Pager, base: int = 0, size: int | None = None) -> None:
        self._pager = pager
        self.base = base
        self.size = pager.size - base if size is None else size

    @classmethod
    def from_bytes(cls, data: bytes) -> RandomAccess:
        return cls(_Pager(io.BytesIO(data), len(data)))

    @staticmethod
    @contextmanager
    def open(path: str | os.PathLike[str] | Path) -> Iterator[RandomAccess]:
        """Open ``path`` read-only. Sources are never opened for writing."""
        with open(path, "rb") as fh:
            yield RandomAccess(_Pager(fh, os.fstat(fh.fileno()).st_size))

    def read_at(self, off: int, n: int) -> bytes:
        """Exactly ``n`` bytes at ``off``; :class:`ParseError` if that is out of bounds."""
        if off < 0 or n < 0 or off + n > self.size:
            raise ParseError(f"read of {n} bytes at {off} outside 0..{self.size}")
        if n > MAX_READ:
            raise ParseError(f"refusing to read {n} bytes at once")
        return self._pager.read(self.base + off, n)

    def read_upto(self, off: int, n: int) -> bytes:
        """Up to ``n`` bytes at ``off``, fewer near the end; empty when ``off`` is past it."""
        if off < 0 or off >= self.size:
            return b""
        return self.read_at(off, min(n, self.size - off))

    def window(self, start: int, length: int) -> RandomAccess:
        """A sub-view of ``length`` bytes at ``start``; offsets inside it start at 0."""
        if start < 0 or length < 0 or start + length > self.size:
            raise ParseError(f"window {start}+{length} outside 0..{self.size}")
        return RandomAccess(self._pager, self.base + start, length)

    def uint(self, off: int, width: int, bo: str = ">") -> int:
        """An unsigned integer of ``width`` bytes (1, 2, 4 or 8) in byte order ``bo``."""
        return int(struct.unpack(bo + _INT_SIZES[width], self.read_at(off, width))[0])

    def u16(self, off: int, bo: str = ">") -> int:
        return self.uint(off, 2, bo)

    def u32(self, off: int, bo: str = ">") -> int:
        return self.uint(off, 4, bo)


class Cursor:
    """Sequential reader over an in-memory payload (iloc, keys, NCTG, ...)."""

    def __init__(self, data: bytes, pos: int = 0, bo: str = ">") -> None:
        self.data = data
        self.pos = pos
        self.bo = bo

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ParseError(f"need {n} bytes at {self.pos}, have {self.remaining}")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def skip(self, n: int) -> None:
        self.take(n)

    def uint(self, width: int) -> int:
        """An unsigned integer of ``width`` bytes; width 0 reads nothing and gives 0."""
        if width == 0:
            return 0
        if width not in _INT_SIZES:
            raise ParseError(f"unsupported integer width {width}")
        return int(struct.unpack(self.bo + _INT_SIZES[width], self.take(width))[0])

    def u8(self) -> int:
        return self.uint(1)

    def u16(self) -> int:
        return self.uint(2)

    def u32(self) -> int:
        return self.uint(4)

    def u64(self) -> int:
        return self.uint(8)
