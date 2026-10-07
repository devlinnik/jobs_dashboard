"""Verify the actual Windows EXE resources, independently of Explorer's cache."""
import ctypes
import struct
from pathlib import Path


def verify_icon_resources(ico, read_resource):
    data = Path(ico).read_bytes()
    reserved, kind, count = struct.unpack_from('<HHH', data)
    if reserved or kind != 1 or not count:
        raise RuntimeError('Invalid source ICO directory')
    group = read_resource(14, 1)  # PyInstaller writes RT_GROUP_ICON number 1.
    if struct.unpack_from('<HHH', group) != (0, 1, count):
        raise RuntimeError('EXE icon group does not match application ICO')
    for index in range(count):
        entry = struct.unpack_from('<BBBBHHII', data, 6 + 16 * index)
        embedded = struct.unpack_from('<BBBBHHIH', group, 6 + 14 * index)
        if embedded[:7] != entry[:7]:
            raise RuntimeError('EXE icon sizes do not match application ICO')
        payload = read_resource(3, embedded[7])  # RT_ICON
        size, offset = entry[6:]
        if len(payload) != size or payload != data[offset:offset + size]:
            raise RuntimeError('EXE contains an outdated or different icon')


def verify_executable_icon(executable, ico):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = ctypes.c_void_p
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.LoadResource.restype = ctypes.c_void_p
    kernel.LockResource.argtypes = [ctypes.c_void_p]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FreeLibrary.restype = wintypes.BOOL
    # Map resources only; do not execute or initialize the application.
    module = kernel.LoadLibraryExW(str(Path(executable).resolve()), None, 0x02 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        def read_resource(kind, identifier):
            resource = kernel.FindResourceW(module, identifier, kind)
            if not resource:
                raise RuntimeError(f'Missing EXE icon resource: {kind}/{identifier}')
            size = kernel.SizeofResource(module, resource)
            loaded = kernel.LoadResource(module, resource)
            address = kernel.LockResource(loaded) if loaded else None
            if not address or not size:
                raise RuntimeError('Cannot read EXE icon resource')
            return ctypes.string_at(address, size)
        verify_icon_resources(ico, read_resource)
    finally:
        kernel.FreeLibrary(module)
