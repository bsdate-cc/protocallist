"""Which port of this computer the panel is served on.

Every Windows account gets a port of its own, worked out from the number of the account, so
that two people who use one computer never find each other's panel under their own address -
not even while one of the two programs is not running. If that port is taken when the program
is started for the first time, the same port a thousand higher is tried, and so on: whatever
the shift, the last three digits are the account's, and no two accounts can meet.
"""
from __future__ import annotations

import ctypes
import socket
import struct
from ctypes import wintypes
from pathlib import Path

BASE = 40700        # far from the ports that web tools of every kind like, and below those Windows hands out itself
STEP, TRIES = 1000, 5


def account() -> int:
    """The number of the Windows account this program runs under: the last part of its security
    identifier - 1001 for the first account made on a computer, 1002 for the next."""
    advapi, kernel = ctypes.windll.advapi32, ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):       # TOKEN_QUERY
        raise ctypes.WinError()
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))                           # 1: TokenUser
        held = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, held, size, ctypes.byref(size)):
            raise ctypes.WinError()
        sid = ctypes.cast(held, ctypes.POINTER(ctypes.c_void_p)).contents.value     # the structure begins with the pointer to it
        parts = advapi.GetSidSubAuthorityCount(sid).contents.value
        return int(advapi.GetSidSubAuthority(sid, parts - 1).contents.value)
    finally:
        kernel.CloseHandle(token)


def candidates(number: int | None = None) -> list[int]:
    """The ports to try for an account, in order: its own, then the same one a thousand higher, and so on."""
    own = BASE + (account() if number is None else number) % STEP
    return [own + STEP * shift for shift in range(TRIES)]


def holder(port: int) -> str:
    """The name of the program that listens on this port of this computer - "node.exe" - or ""
    when nobody does, or when Windows does not say."""
    try:
        table, kernel = ctypes.windll.iphlpapi.GetExtendedTcpTable, ctypes.windll.kernel32
        size = wintypes.DWORD(0)
        table(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)            # 3: those that listen, with their processes
        held = ctypes.create_string_buffer(size.value)
        if table(held, ctypes.byref(size), False, socket.AF_INET, 3, 0) != 0:
            return ""
        for row in range(struct.unpack_from("<I", held, 0)[0]):
            state, address, local, far, far_port, process = struct.unpack_from("<6I", held, 4 + 24 * row)
            if socket.ntohs(local & 0xFFFF) != port:
                continue
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                          ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            opened = kernel.OpenProcess(0x1000, False, process)                 # as little as it takes to ask for its name
            if not opened:
                return ""                                                       # another user's, or gone already
            try:
                name, length = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if not kernel.QueryFullProcessImageNameW(opened, 0, name, ctypes.byref(length)):
                    return ""
                return Path(name.value).name
            finally:
                kernel.CloseHandle(opened)
    except (OSError, AttributeError, struct.error):
        pass
    return ""
