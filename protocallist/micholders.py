"""What Windows knows about a microphone: which programs hold it now, and which playback
device belongs to the same headset.

Windows keeps, for every microphone, the sessions of the programs recording from it. A
session that is active is a program with the microphone open - that is how a conversation
is noticed without asking the conversation program anything.

Call these from one thread. The first import of comtypes prepares the thread that made
it; any other thread says this_thread() before its first question and leave_thread() when
it is done.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_ACTIVE = 1                                 # AudioSessionStateActive
# The piece of hardware an endpoint hangs on: the microphone and the earphones of one headset share it.
_HARDWARE = "{B3F8FA53-0004-438E-9003-51A46E139BFC} 2"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_user32.GetForegroundWindow.restype = wintypes.HWND
_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _endpoints(flow: str):
    """The working devices of one direction - "capture" or "render" - as pycaw shows them, with the raw device."""
    import comtypes
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import AudioUtilities, DEVICE_STATE, EDataFlow, IMMDeviceEnumerator

    enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                           comtypes.CLSCTX_INPROC_SERVER)
    direction = EDataFlow.eCapture if flow == "capture" else EDataFlow.eRender
    collection = enumerator.EnumAudioEndpoints(direction.value, DEVICE_STATE.ACTIVE.value)
    for n in range(collection.GetCount()):
        raw = collection.Item(n)
        yield AudioUtilities.CreateDevice(raw), raw


def this_thread() -> None:
    """Prepare the calling thread for the questions below."""
    import comtypes

    comtypes.CoInitialize()


def leave_thread() -> None:
    import comtypes

    comtypes.CoUninitialize()


def microphones() -> list[str]:
    """The microphones of this computer, as Windows names them."""
    return [device.FriendlyName for device, _ in _endpoints("capture")]


def program_of(pid: int) -> str:
    """The file name of a process - "Zoom.exe" - or "" if it cannot be asked."""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value)
    finally:
        _kernel32.CloseHandle(handle)


def holders(microphone: str) -> list[str] | None:
    """The programs that hold this microphone now, by file name, ourselves left out;
    None if there is no such microphone."""
    import comtypes
    from pycaw.pycaw import IAudioSessionControl2, IAudioSessionManager2

    for device, raw in _endpoints("capture"):
        if device.FriendlyName != microphone:
            continue
        manager = raw.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
        sessions = manager.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
        found: list[str] = []
        for n in range(sessions.GetCount()):
            session = sessions.GetSession(n).QueryInterface(IAudioSessionControl2)
            pid = session.GetProcessId()
            if session.GetState() != _ACTIVE or pid in (0, os.getpid()):
                continue
            name = program_of(pid)
            if name and name not in found:
                found.append(name)
        return found
    return None


def speaker_of(microphone: str) -> str | None:
    """The playback device on the same piece of hardware as this microphone - the earphones
    of the headset - or None if there is none."""
    hardware = next((device.properties.get(_HARDWARE) for device, _ in _endpoints("capture")
                     if device.FriendlyName == microphone), None)
    if not hardware:
        return None
    return next((device.FriendlyName for device, _ in _endpoints("render")
                 if device.properties.get(_HARDWARE) == hardware), None)


def window_title(program: str) -> str:
    """The title of a visible window of this program: the one in front if it is this
    program's, else the first that has a title; "" if there is none."""
    titles: list[tuple[int, str]] = []

    def visit(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if program_of(pid.value).lower() == program.lower():
            buffer = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buffer, length + 1)
            titles.append((hwnd, buffer.value))
        return True

    _user32.EnumWindows(_ENUM(visit), 0)
    front = _user32.GetForegroundWindow()
    return next((title for hwnd, title in titles if hwnd == front), titles[0][1] if titles else "")
