"""Read-only Weixin 4.1.x key recovery; diagnostics never contain key material.

The scanner locates a loaded Weixin module, derives candidate masks from its
executable sections, and checks candidate heap strings against an authenticated
database page. It never injects code or writes to the target process.
"""
from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes as wt
import hashlib
import io
import os
from pathlib import Path
import re
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .key_scan_common import verify_enc_key


ACCESS = 0x0400 | 0x0010
CHUNK_SIZE = 1024 * 1024
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
STRING_TAIL = struct.pack("<QQQ", 0, 32, 47)
MASK_PATTERN = re.compile(
    rb"\x48\xba(.{8}).{3,8}?\x48\xba(.{8}).{3,8}?"
    rb"\x48\xba(.{8}).{3,8}?\x48\xba(.{8}).{3,8}?\x48\x85\xc0",
    re.DOTALL,
)


def static_masks(path: str | os.PathLike[str]) -> list[bytes]:
    """Return candidate constants from executable PE sections only."""
    with open(path, "rb") as handle:
        dos = handle.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("Invalid DOS header")
        handle.seek(struct.unpack_from("<I", dos, 60)[0])
        header = handle.read(24)
        if len(header) != 24 or header[:4] != b"PE\0\0":
            raise ValueError("Invalid PE header")
        machine, section_count = struct.unpack_from("<HH", header, 4)
        if machine != 0x8664 or not 0 < section_count < 100:
            raise ValueError("Expected an x64 PE image")
        optional_size = struct.unpack_from("<H", header, 20)[0]
        handle.seek(optional_size, 1)
        table = handle.read(section_count * 40)
        if len(table) != section_count * 40:
            raise ValueError("Truncated PE section table")
        file_size = os.fstat(handle.fileno()).st_size
        result: list[bytes] = []
        for index in range(section_count):
            size, offset = struct.unpack_from("<II", table, index * 40 + 16)
            flags = struct.unpack_from("<I", table, index * 40 + 36)[0]
            if not flags & 0x20000000:
                continue
            if offset + size > file_size:
                raise ValueError("Truncated executable PE section")
            handle.seek(offset)
            remaining = size
            tail = b""
            while remaining:
                block = handle.read(min(CHUNK_SIZE, remaining))
                if not block:
                    raise ValueError("Truncated executable PE data")
                remaining -= len(block)
                data = tail + block
                for match in MASK_PATTERN.finditer(data):
                    mask = b"".join(match.groups())
                    if mask not in result:
                        result.append(mask)
                tail = data[-96:]
    return result


def key_pointers(data: bytes, base: int = 0):
    """Yield aligned pointers to 32-byte heap string objects."""
    start = 0
    while True:
        position = data.find(STRING_TAIL, start)
        if position < 0:
            return
        start = position + 1
        if position < 8 or (base + position - 8) % 8 or data[position - 2:position] != b"\0\0":
            continue
        pointer = struct.unpack_from("<Q", data, position - 8)[0]
        if 0x10000 <= pointer < 0x7FFFFFFF0000:
            yield pointer


def derive_key(password: bytes, page: bytes) -> bytes | None:
    """Derive and authenticate one SQLCipher key from a database page."""
    if len(password) != 32 or len(page) != 4096:
        return None
    derived = hashlib.pbkdf2_hmac("sha512", password, page[:16], 256000, dklen=32)
    return derived if verify_enc_key(derived, page) else None


def candidates() -> list[tuple[int, str, int]]:
    """Return exact-name Weixin/WeChat processes, largest first."""
    completed = subprocess.run(
        ["tasklist.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=True,
    )
    selected: list[tuple[int, str, int]] = []
    for row in csv.reader(io.StringIO(completed.stdout)):
        if len(row) >= 5 and row[0].casefold() in {"weixin.exe", "wechat.exe"}:
            memory = int("".join(character for character in row[4] if character.isdecimal()) or "0")
            selected.append((int(row[1]), row[0], memory))
    return sorted(selected, key=lambda item: item[2], reverse=True)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class SystemInfo(ctypes.Structure):
    _fields_ = [
        ("architecture", wt.DWORD),
        ("page_size", wt.DWORD),
        ("minimum", ctypes.c_void_p),
        ("maximum", ctypes.c_void_p),
        ("mask", ctypes.c_size_t),
        ("cpus", wt.DWORD),
        ("type", wt.DWORD),
        ("granularity", wt.DWORD),
        ("level", wt.WORD),
        ("revision", wt.WORD),
    ]


class ProcessMemory:
    """Read-only process-memory helper with bounded chunks and diagnostics."""

    def __init__(self, pid: int, report):
        self.pid, self.report, self.handle = pid, report, None
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.p = ctypes.WinDLL("psapi", use_last_error=True)
        self.k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.k.OpenProcess.restype = wt.HANDLE
        self.k.CloseHandle.argtypes = [wt.HANDLE]
        self.k.CloseHandle.restype = wt.BOOL
        self.k.ReadProcessMemory.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.k.ReadProcessMemory.restype = wt.BOOL
        self.k.VirtualQueryEx.argtypes = [
            wt.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(MBI),
            ctypes.c_size_t,
        ]
        self.k.VirtualQueryEx.restype = ctypes.c_size_t
        self.k.GetNativeSystemInfo.argtypes = [ctypes.POINTER(SystemInfo)]
        self.k.GetNativeSystemInfo.restype = None
        self.p.EnumProcessModulesEx.argtypes = [
            wt.HANDLE,
            ctypes.POINTER(wt.HMODULE),
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
            wt.DWORD,
        ]
        self.p.EnumProcessModulesEx.restype = wt.BOOL
        self.p.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
        self.p.GetModuleFileNameExW.restype = wt.DWORD
        self.read_errors: dict[int, int] = {}
        self.read_bytes = 0

    def __enter__(self):
        self.handle = self.k.OpenProcess(ACCESS, False, self.pid)
        error = ctypes.get_last_error() if not self.handle else 0
        self.report(
            "process_open",
            pid=self.pid,
            api="OpenProcess",
            access=ACCESS,
            success=bool(self.handle),
            error=error,
        )
        if not self.handle:
            raise OSError(error, "OpenProcess failed")
        return self

    def __exit__(self, *args):
        if self.handle:
            ok = self.k.CloseHandle(self.handle)
            error = ctypes.get_last_error() if not ok else 0
            self.handle = None
            self.report(
                "process_close",
                pid=self.pid,
                success=bool(ok),
                error=error,
                read_bytes=self.read_bytes,
                read_errors=self.read_errors,
            )

    def module_path(self) -> Path | None:
        modules = (wt.HMODULE * 2048)()
        needed = wt.DWORD()
        ok = self.p.EnumProcessModulesEx(
            self.handle,
            modules,
            ctypes.sizeof(modules),
            ctypes.byref(needed),
            3,
        )
        error = ctypes.get_last_error() if not ok else 0
        if not ok or needed.value > ctypes.sizeof(modules):
            self.report(
                "module_enumeration",
                pid=self.pid,
                api="EnumProcessModulesEx",
                error=error,
                success=False,
            )
            return None
        count = needed.value // ctypes.sizeof(wt.HMODULE)
        for module in modules[:count]:
            name = ctypes.create_unicode_buffer(32768)
            size = self.p.GetModuleFileNameExW(self.handle, module, name, len(name))
            if not size:
                error = ctypes.get_last_error()
                self.report(
                    "module_path",
                    pid=self.pid,
                    api="GetModuleFileNameExW",
                    success=False,
                    error=error,
                )
                continue
            if Path(name.value).name.casefold() in {"weixin.dll", "wechatwin.dll"}:
                return Path(name.value)
        self.report("module_enumeration", pid=self.pid, success=False, error=0)
        return None

    def regions(self, private_only: bool = True):
        info = SystemInfo()
        self.k.GetNativeSystemInfo(ctypes.byref(info))
        address, maximum = info.minimum, info.maximum
        count = skipped = 0
        while address <= maximum:
            mbi = MBI()
            result = self.k.VirtualQueryEx(
                self.handle,
                address,
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            error = ctypes.get_last_error() if not result else 0
            if not result:
                self.report(
                    "region_query",
                    pid=self.pid,
                    api="VirtualQueryEx",
                    error=error,
                    completed=False,
                    regions=count,
                )
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if size == 0 or base + size <= address:
                raise RuntimeError("Non-advancing memory region")
            readable = (
                mbi.State == 0x1000
                and not mbi.Protect & 0x100
                and mbi.Protect & 0xFF in READABLE
            )
            if readable and (not private_only or mbi.Type == 0x20000):
                count += 1
                yield base, size
            else:
                skipped += 1
            address = base + size
        else:
            self.report(
                "region_query",
                pid=self.pid,
                completed=True,
                regions=count,
                skipped_regions=skipped,
            )

    def read(self, address: int, size: int) -> bytes:
        data = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = self.k.ReadProcessMemory(
            self.handle,
            address,
            data,
            size,
            ctypes.byref(read),
        )
        error = ctypes.get_last_error() if not ok else 0
        if not ok:
            self.read_errors[error] = self.read_errors.get(error, 0) + 1
            if self.read_errors[error] == 1:
                self.report(
                    "memory_read",
                    pid=self.pid,
                    api="ReadProcessMemory",
                    error=error,
                    requested_bytes=size,
                    returned_bytes=read.value,
                    success=False,
                )
        self.read_bytes += read.value
        return data.raw[:read.value]

    def chunks(self, private_only: bool = True, overlap: int = 31):
        for base, size in self.regions(private_only):
            for offset in range(0, size, CHUNK_SIZE):
                length = min(CHUNK_SIZE + overlap, size - offset)
                data = self.read(base + offset, length)
                if data:
                    yield base + offset, data


def recover_modern_keys(db_files, report):
    """Recover keys authenticated against the caller's selected databases."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("The Weixin 4.1.x scanner requires 64-bit Python")
    ordered = sorted(
        db_files,
        key=lambda database: (database[0].replace("\\", "/") != "contact/contact.db", database[2]),
    )
    if not ordered:
        return {}
    sample = ordered[0][4]
    selected = candidates()
    report("candidate_processes", count=len(selected), pids=[item[0] for item in selected])
    if not selected:
        raise RuntimeError("Weixin.exe or WeChat.exe is not running")

    tested: set[bytes] = set()
    for pid, name, _ in selected:
        report("candidate", pid=pid, name=name)
        try:
            with ProcessMemory(pid, report) as process:
                path = process.module_path()
                if path is None:
                    continue
                masks = static_masks(path)
                report("static_signature", pid=pid, matches=len(masks))
                if not masks:
                    continue
                pointers: set[int] = set()
                for base, data in process.chunks():
                    pointers.update(key_pointers(data, base))
                raw_candidates: set[bytes] = set()
                for pointer in pointers:
                    value = process.read(pointer, 32)
                    if len(value) == 32:
                        raw_candidates.add(value)
                report(
                    "candidate_structures",
                    pid=pid,
                    matches=len(pointers),
                    unique_values=len(raw_candidates),
                )
                raw_candidates = sorted(
                    raw_candidates,
                    key=lambda value: (-len(set(value)), sum(32 <= item <= 126 for item in value)),
                )
                passwords: list[bytes] = []
                for raw in raw_candidates:
                    for mask in masks:
                        password = bytes(left ^ right for left, right in zip(raw, mask))
                        if password not in tested:
                            tested.add(password)
                            passwords.append(password)
                report("kdf_start", pid=pid, candidates=len(passwords))
                valid = None
                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
                    for offset in range(0, len(passwords), 16):
                        batch = passwords[offset:offset + 16]
                        results = list(pool.map(lambda password: derive_key(password, sample), batch))
                        for password, derived in zip(batch, results):
                            if derived is not None:
                                valid = password
                                break
                        report(
                            "kdf_progress",
                            pid=pid,
                            tested=min(offset + 16, len(passwords)),
                            total=len(passwords),
                            success=valid is not None,
                            elapsed_seconds=round(time.monotonic() - started, 2),
                        )
                        if valid is not None:
                            break
                if valid is None:
                    continue
                keys = {}
                for _, _, _, salt, page in db_files:
                    derived = derive_key(valid, page)
                    if derived is not None:
                        keys[salt] = derived.hex()
                report("database_key_validation", pid=pid, verified=len(keys), total=len(db_files))
                return keys
        except (OSError, ValueError, RuntimeError) as exc:
            report(
                "candidate_failure",
                pid=pid,
                exception=type(exc).__name__,
                error=getattr(exc, "winerror", None) or getattr(exc, "errno", None),
            )
    return {}
