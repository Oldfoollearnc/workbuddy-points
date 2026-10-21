# -*- coding: utf-8 -*-
"""Windows DPAPI 加解密（用于加密保存登录态，避免明文落盘）。"""
import ctypes
import ctypes.wintypes as wt
import json

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(text: str) -> bytes:
    data = text.encode("utf-8")
    inb, outb = _blob(data), DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("CryptProtectData failed: %s" % ctypes.get_last_error())
    try:
        return _blob_bytes(outb)
    finally:
        kernel32.LocalFree(outb.pbData)


def unprotect(raw: bytes) -> str:
    inb, outb = _blob(raw), DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb)):
        raise OSError("CryptUnprotectData failed: %s" % ctypes.get_last_error())
    try:
        return _blob_bytes(outb).decode("utf-8")
    finally:
        kernel32.LocalFree(outb.pbData)


def save_json(path: str, obj) -> None:
    with open(path, "wb") as f:
        f.write(protect(json.dumps(obj, ensure_ascii=False)))


def load_json(path: str):
    with open(path, "rb") as f:
        return json.loads(unprotect(f.read()))
