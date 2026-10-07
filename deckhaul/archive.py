"""Read-only access to mod containers: ZIP .scs, HashFS v1/v2 .scs and plain folders.

Every container exposes the same small surface:
  * read(path)        -> bytes | None
  * file_hashes()     -> {cityhash64(path): path | None}, files only
  * root_names()      -> names in the archive root (for layout checks)

HashFS stores only hashes. Paths are recovered by walking directory
listings; a "locked" mod without listings still yields hashes, which is
enough to find overlaps between two mods.
"""

from __future__ import annotations

import mmap
import os
import struct
import zipfile
import zlib
from typing import Dict, List, Optional

from .cityhash import hash_path

HASHFS_MAGIC = b"SCS#"
# Some mods are "protected" with entries that unpack to gigabytes (zip bombs).
# The game never touches them; we must not either. Everything DeckHaul reads
# from an archive (manifest, description, icon, directory listings) is small.
MAX_READ = 32 << 20
CRC_BUDGET = 256 << 20
ZIP_MAGIC = b"PK"


class ArchiveError(Exception):
    pass


def _norm(path: str) -> str:
    path = path.replace("\\", "/").strip("/")
    return path


class Container:
    kind = "unknown"

    def read(self, path: str) -> Optional[bytes]:
        raise NotImplementedError

    def file_hashes(self) -> Dict[int, Optional[str]]:
        raise NotImplementedError

    def root_names(self) -> List[str]:
        raise NotImplementedError

    def content_hashes(self, root_meta=()):
        """(hashes of files below the archive root, whether the paths are known).
        Root-level files (manifest, icon, description) never conflict, so they are left out."""
        out, known = [], True
        root = {hash_path(n) for n in root_meta}
        for h, p in self.file_hashes().items():
            if p is None:
                known = False
                if h in root:
                    continue
            elif "/" not in p:
                continue
            out.append(h)
        return out, known

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------- folder

class FolderContainer(Container):
    kind = "folder"

    def __init__(self, root: str):
        self.root = root

    def read(self, path: str) -> Optional[bytes]:
        full = os.path.join(self.root, *_norm(path).split("/"))
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def file_hashes(self) -> Dict[int, Optional[str]]:
        out: Dict[int, Optional[str]] = {}
        for dirpath, _dirs, files in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            for name in files:
                rel = name if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + name
                out[hash_path(rel)] = rel
        return out

    def root_names(self) -> List[str]:
        return sorted(os.listdir(self.root))


# ------------------------------------------------------------------------ zip

class ZipContainer(Container):
    kind = "zip"

    def __init__(self, path: str):
        self.path = path
        self._raw: Optional[Dict[str, tuple]] = None
        try:
            self.zf: Optional[zipfile.ZipFile] = zipfile.ZipFile(path)
            self.names = [_norm(n) for n in self.zf.namelist() if not n.endswith("/")]
            self._map = {_norm(n): n for n in self.zf.namelist()}
        except (zipfile.BadZipFile, OSError, ValueError):
            # Some .scs zips ship a broken central directory on purpose.
            # The game reads local headers, so we do the same.
            self.zf = None
            self._raw = _scan_local_headers(path)
            self.names = list(self._raw)
            if not self.names:
                raise ArchiveError("ZIP-архив повреждён: не найдено ни одного файла")

    def read(self, path: str) -> Optional[bytes]:
        key = _norm(path)
        if self.zf is not None:
            real = self._map.get(key)
            if real is None:
                # Case-insensitive fallback: Windows-made mods are inconsistent.
                for k, v in self._map.items():
                    if k.lower() == key.lower():
                        real = v
                        break
            if real is None:
                return None
            try:
                info = self.zf.getinfo(real)
                if info.file_size > MAX_READ:
                    raise ArchiveError(f"файл {key} внутри архива слишком большой")
                if info.flag_bits & 0x1:
                    # "Protected" mods set the encryption flag on plain data. The game
                    # ignores the flag; Python refuses to read. Read it the game's way.
                    return self._read_flagged(info, key)
                with self.zf.open(info) as fh:
                    data = fh.read(MAX_READ + 1)
                if len(data) > MAX_READ:
                    raise ArchiveError(f"файл {key} внутри архива слишком большой")
                return data
            except (zipfile.BadZipFile, zlib.error, OSError, NotImplementedError, RuntimeError) as exc:
                raise ArchiveError(f"не удалось распаковать {key}: {exc}")
        entry = self._raw.get(key) if self._raw else None
        if entry is None:
            return None
        return _read_raw_entry(self.path, entry)

    def _read_flagged(self, info, key: str) -> bytes:
        with open(self.path, "rb") as fh:
            fh.seek(info.header_offset)
            head = fh.read(30)
            if len(head) < 30 or head[:4] != b"PK\x03\x04":
                raise ArchiveError(f"не удалось прочитать {key}: повреждён заголовок")
            nlen, xlen = struct.unpack_from("<HH", head, 26)
            start = info.header_offset + 30 + nlen + xlen
        try:
            return _read_raw_entry(self.path, (start, info.compress_size, info.file_size, info.compress_type))
        except (zlib.error, ArchiveError):
            raise ArchiveError(f"{key} внутри архива действительно зашифрован, прочитать его нельзя")

    def test(self) -> Optional[str]:
        """Return the first broken member name, or None if CRCs are fine.
        Reads at most CRC_BUDGET bytes and skips members that look like zip bombs."""
        if self.zf is None:
            return None
        budget = CRC_BUDGET
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            ratio = info.file_size / max(info.compress_size, 1)
            if info.file_size > budget or (info.file_size > (16 << 20) and ratio > 100):
                return None          # not worth it, or a deliberate trap: stop checking
            budget -= info.file_size
            if info.flag_bits & 0x1:
                continue
            try:
                with self.zf.open(info) as fh:
                    while fh.read(1 << 20):
                        pass
            except Exception:  # zlib errors surface as many types
                return info.filename
        return None

    def file_hashes(self) -> Dict[int, Optional[str]]:
        return {hash_path(n): n for n in self.names}

    def root_names(self) -> List[str]:
        seen = []
        for n in self.names:
            top = n.split("/", 1)[0]
            if top not in seen:
                seen.append(top)
        return seen

    def close(self) -> None:
        if self.zf is not None:
            self.zf.close()


def _scan_local_headers(path: str) -> Dict[str, tuple]:
    out: Dict[str, tuple] = {}
    with open(path, "rb") as fh:
        try:
            data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)  # no copy of a 2 GB file
        except (ValueError, OSError):
            data = fh.read()
    pos = 0
    while True:
        pos = data.find(b"PK\x03\x04", pos)
        if pos < 0 or pos + 30 > len(data):
            break
        (_sig, _ver, flags, method, _t, _d, _crc, csize, usize, nlen, xlen) = struct.unpack_from(
            "<IHHHHHIIIHH", data, pos
        )
        name = data[pos + 30 : pos + 30 + nlen].decode("utf-8", "replace")
        start = pos + 30 + nlen + xlen
        if not name.endswith("/") and not (flags & 0x08 and csize == 0):
            out[_norm(name)] = (start, csize, usize, method)
        pos = start + max(csize, 0) if csize else pos + 4
    return out


def _read_raw_entry(path: str, entry: tuple) -> bytes:
    start, csize, _usize, method = entry
    with open(path, "rb") as fh:
        fh.seek(start)
        blob = fh.read(csize)
    if method == 0:
        return blob[:MAX_READ]
    if method == 8:
        data = zlib.decompressobj(-15).decompress(blob, MAX_READ + 1)
        if len(data) > MAX_READ:
            raise ArchiveError("файл внутри архива слишком большой")
        return data
    raise ArchiveError(f"неизвестный метод сжатия ZIP: {method}")


# --------------------------------------------------------------------- hashfs

class _Entry:
    __slots__ = ("hash", "offset", "size", "csize", "compressed", "is_dir", "readable")

    def __init__(self, h, offset, size, csize, compressed, is_dir, readable=True):
        self.hash = h
        self.offset = offset
        self.size = size
        self.csize = csize
        self.compressed = compressed
        self.is_dir = is_dir
        self.readable = readable


class HashFsContainer(Container):
    def __init__(self, path: str):
        self.path = path
        self.fh = open(path, "rb")
        try:
            head = self.fh.read(64)
            if head[:4] != HASHFS_MAGIC:
                raise ArchiveError("не HashFS-архив")
            self.version, self.salt = struct.unpack_from("<HH", head, 4)
            method = head[8:12]
            if method != b"CITY":
                raise ArchiveError(f"неизвестный метод хеширования {method!r}")
            self.entries: Dict[int, _Entry] = {}
            self._extra_dirs: List[_Entry] = []
            if self.version == 1:
                self.kind = "hashfs1"
                self._parse_v1(head)
            elif self.version == 2:
                self.kind = "hashfs2"
                self._parse_v2(head)
            else:
                raise ArchiveError(f"HashFS версии {self.version} не поддерживается")
        except (struct.error, zlib.error, OSError) as exc:
            self.fh.close()
            raise ArchiveError(f"архив повреждён: {exc}")
        except ArchiveError:
            self.fh.close()
            raise
        self._paths: Optional[Dict[int, str]] = None

    # -- parsing
    def _parse_v1(self, head: bytes) -> None:
        count, start = struct.unpack_from("<IQ", head, 12)
        size = os.path.getsize(self.path)
        if start + count * 32 > size:
            start = size - count * 32  # some packers put the table at the end
        self.fh.seek(start)
        table = self.fh.read(count * 32)
        if len(table) < count * 32:
            raise ArchiveError("таблица файлов обрезана")
        for i in range(count):
            h, off, flags, _crc, usize, csize = struct.unpack_from("<QQIIII", table, i * 32)
            e = _Entry(h, off, usize, csize, bool(flags & 2), bool(flags & 1))
            if h in self.entries:
                if e.is_dir and self.entries[h].is_dir:
                    self._extra_dirs.append(e)
                continue
            self.entries[h] = e

    def _parse_v2(self, head: bytes) -> None:
        (count, et_len, _meta_count, mt_len, et_start, mt_start) = struct.unpack_from(
            "<IIIIQQ", head, 12
        )
        self.fh.seek(et_start)
        et = zlib.decompress(self.fh.read(et_len))
        self.fh.seek(mt_start)
        mt = zlib.decompress(self.fh.read(mt_len))
        for i in range(len(et) // 16):
            h, meta_index, meta_count, _flags = struct.unpack_from("<QIHH", et, i * 16)
            pos = meta_index * 4
            chunk_types = []
            for _ in range(meta_count):
                chunk_types.append(mt[pos + 3])
                pos += 4
            if not chunk_types:
                continue
            first = chunk_types[0]
            if first in (128, 129):  # plain file, directory
                e = self._main_meta(mt, pos, h, is_dir=(first == 129))
                self.entries[h] = e
            else:
                # Packed textures and other special chunks: present, but we
                # never need their bytes.
                self.entries[h] = _Entry(h, 0, 0, 0, False, False, readable=False)

    @staticmethod
    def _main_meta(mt: bytes, pos: int, h: int, is_dir: bool) -> _Entry:
        b = mt[pos : pos + 16]
        csize = b[0] | (b[1] << 8) | (b[2] << 16) | ((b[3] & 0x0F) << 24)
        compressed = bool(b[3] & 0x10)
        usize = b[4] | (b[5] << 8) | (b[6] << 16) | ((b[7] & 0x0F) << 24)
        offset_block = struct.unpack_from("<I", b, 12)[0]
        return _Entry(h, offset_block * 16, usize, csize, compressed, is_dir)

    # -- content
    def _content(self, e: _Entry) -> bytes:
        if not e.readable:
            raise ArchiveError("содержимое этого файла упаковано особым образом")
        if e.size > MAX_READ or e.csize > MAX_READ:
            raise ArchiveError("файл внутри архива слишком большой")
        self.fh.seek(e.offset)
        if e.compressed:
            d = zlib.decompressobj()
            data = d.decompress(self.fh.read(e.csize), MAX_READ + 1)
            if len(data) > MAX_READ or d.unconsumed_tail:
                raise ArchiveError("файл внутри архива слишком большой")
            return data
        return self.fh.read(e.size)

    def _hash(self, path: str) -> int:
        return hash_path(_norm(path), self.salt)

    def read(self, path: str) -> Optional[bytes]:
        e = self.entries.get(self._hash(path))
        if e is None or e.is_dir:
            return None
        try:
            return self._content(e)
        except zlib.error as exc:
            raise ArchiveError(f"не удалось распаковать {path}: {exc}")

    def _listing(self, path: str):
        h = self._hash(path)
        e = self.entries.get(h)
        if e is None or not e.is_dir:
            return None
        dirs, files = [], []
        for part in [e] + [x for x in self._extra_dirs if x.hash == h]:
            raw = self._content(part)
            if self.version == 1:
                for line in raw.decode("utf-8", "replace").splitlines():
                    if not line:
                        continue
                    (dirs if line.startswith("*") else files).append(line.lstrip("*"))
            else:
                n = struct.unpack_from("<I", raw, 0)[0]
                lens = raw[4 : 4 + n]
                p = 4 + n
                for ln in lens:
                    s = raw[p : p + ln].decode("utf-8", "replace")
                    p += ln
                    (dirs if s.startswith("/") else files).append(s.lstrip("/"))
        return dirs, files

    def _walk(self) -> Dict[int, str]:
        if self._paths is not None:
            return self._paths
        paths: Dict[int, str] = {}
        stack = [""]
        seen = set()
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            try:
                listing = self._listing(d)
            except (ArchiveError, zlib.error, struct.error):
                listing = None
            if listing is None:
                continue
            dirs, files = listing
            for f in files:
                full = f if not d else d + "/" + f
                paths[self._hash(full)] = full
            for sub in dirs:
                stack.append(sub if not d else d + "/" + sub)
        self._paths = paths
        return paths

    def file_hashes(self) -> Dict[int, Optional[str]]:
        known = self._walk()
        out: Dict[int, Optional[str]] = {}
        for h, e in self.entries.items():
            if e.is_dir:
                continue
            out[h] = known.get(h)
        return out

    def root_names(self) -> List[str]:
        try:
            listing = self._listing("")
        except (ArchiveError, zlib.error, struct.error):
            listing = None
        if listing is None:
            return []
        dirs, files = listing
        return dirs + files

    def content_hashes(self, root_meta=()):
        # The entry table already holds the path hashes; hashing every path
        # again in Python would take minutes on a big map mod.
        files = {h for h, e in self.entries.items() if not e.is_dir}
        root_files = set(root_meta)
        try:
            listing = self._listing("")
        except (ArchiveError, zlib.error, struct.error):
            listing = None
        if listing is not None:
            root_files.update(listing[1])
        files -= {self._hash(n) for n in root_files}
        return sorted(files), listing is not None

    def has_listing(self) -> bool:
        return self._hash("") in self.entries

    def close(self) -> None:
        self.fh.close()


# ---------------------------------------------------------------------- entry

def detect_kind(path: str) -> str:
    if os.path.isdir(path):
        return "folder"
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic == HASHFS_MAGIC:
        return "hashfs"
    if magic[:2] == ZIP_MAGIC:
        return "zip"
    return "unknown"


def open_container(path: str) -> Container:
    kind = detect_kind(path)
    if kind == "folder":
        return FolderContainer(path)
    if kind == "hashfs":
        return HashFsContainer(path)
    if kind == "zip":
        return ZipContainer(path)
    raise ArchiveError("файл не похож на мод: это не ZIP и не HashFS")
