"""Native Windows 'Select Folder' dialog via IFileOpenDialog (Vista+), ctypes only.

The engine runs as a headless sidecar (no console, no GUI toolkit - tkinter is
excluded from the frozen build), so we drive the COM shell dialog directly.
The dialog is owned by the current foreground window so it appears on top of the
app rather than behind it. Returns the chosen path, or None if the user cancels.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_long, c_ulong, c_void_p, c_wchar_p
from ctypes.wintypes import DWORD, HWND

_ole32 = ctypes.windll.ole32
_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = HWND          # HWND is pointer-sized; don't truncate on 64-bit
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.CoTaskMemFree.restype = None

# CLSID/IID as strings; parsed via CLSIDFromString so we avoid hand-packing GUIDs.
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}"
_IID_IFileOpenDialog = "{d57c7288-d4ad-4768-be02-9d969532d960}"

_CLSCTX_INPROC_SERVER = 1
_COINIT_APARTMENTTHREADED = 0x2
_FOS_PICKFOLDERS = 0x20
_FOS_FORCEFILESYSTEM = 0x40
_SIGDN_FILESYSPATH = 0x80058000
_S_OK = 0
_S_FALSE = 1

# IFileOpenDialog vtable slots (IUnknown 0-2, IModalWindow 3, IFileDialog 4-26).
_SHOW = 3
_SET_OPTIONS = 9
_GET_OPTIONS = 10
_GET_RESULT = 20
_ISHELLITEM_GET_DISPLAY_NAME = 5


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    if _ole32.CLSIDFromString(c_wchar_p(s), byref(g)) != _S_OK:
        raise OSError(f"bad GUID {s}")
    return g


def _vtbl(ptr, index, restype, *argtypes):
    """Bind vtable slot `index` on COM interface pointer `ptr` as a callable."""
    vtable = ctypes.cast(ptr, POINTER(c_void_p))[0]
    fn = ctypes.cast(vtable, POINTER(c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)


def _release(ptr) -> None:
    _vtbl(ptr, 2, c_ulong)(ptr)


def pick_folder() -> str | None:
    """Show the native folder picker; return the selected path, or None if cancelled."""
    hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    inited = hr in (_S_OK, _S_FALSE)   # skip CoUninitialize if COM was already up as another model
    try:
        clsid = _guid(_CLSID_FileOpenDialog)
        iid = _guid(_IID_IFileOpenDialog)
        pfd = c_void_p()
        if _ole32.CoCreateInstance(byref(clsid), None, _CLSCTX_INPROC_SERVER,
                                   byref(iid), byref(pfd)) != _S_OK or not pfd:
            raise OSError("could not create the folder dialog")
        try:
            opts = DWORD()
            _vtbl(pfd, _GET_OPTIONS, c_long, POINTER(DWORD))(pfd, byref(opts))
            _vtbl(pfd, _SET_OPTIONS, c_long, DWORD)(
                pfd, opts.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM)

            owner = _user32.GetForegroundWindow()
            if _vtbl(pfd, _SHOW, c_long, HWND)(pfd, owner) != _S_OK:
                return None   # user cancelled (or closed the dialog)

            psi = c_void_p()
            if _vtbl(pfd, _GET_RESULT, c_long, POINTER(c_void_p))(pfd, byref(psi)) != _S_OK or not psi:
                return None
            try:
                name = c_wchar_p()
                if _vtbl(psi, _ISHELLITEM_GET_DISPLAY_NAME, c_long, DWORD, POINTER(c_wchar_p))(
                        psi, _SIGDN_FILESYSPATH, byref(name)) != _S_OK:
                    return None
                try:
                    return name.value
                finally:
                    _ole32.CoTaskMemFree(name)
            finally:
                _release(psi)
        finally:
            _release(pfd)
    finally:
        if inited:
            _ole32.CoUninitialize()
