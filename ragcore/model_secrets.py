"""Authenticated storage for workspace model credentials."""
import base64
import json
import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(ValueError):
    pass


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(data)
    source, dest = Blob(len(data), ctypes.addressof(buf)), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    # Both functions accept NULL for the optional description and entropy.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError(ctypes.get_last_error(), "DPAPI failed")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(dest.data))


def _fernet(directory, create):
    path = Path(directory) / ".model-credentials.key"
    if create and not path.exists():
        fd, name = tempfile.mkstemp(dir=directory, prefix=".model-key-")
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(Fernet.generate_key())
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(name, path)  # Publish only the complete 0600 file, never replace a key.
            except FileExistsError:
                pass
        finally:
            os.unlink(name)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        meta = os.fstat(source.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise PermissionError("Invalid key file permissions")
        return Fernet(source.read(45))


def seal(directory, kind, base, token):
    payload = json.dumps([kind, base, token]).encode()
    try:
        if os.name == "nt":
            return "dpapi:" + base64.b64encode(_dpapi(payload, True)).decode("ascii")
        return "fernet:" + _fernet(directory, create=True).encrypt(payload).decode("ascii")
    except (OSError, ValueError):
        raise CredentialError("无法安全保存密钥，请检查服务账户对数据目录的访问权限。") from None


def unseal(directory, kind, base, encoded):
    try:
        if os.name == "nt" and encoded.startswith("dpapi:"):
            raw = _dpapi(base64.b64decode(encoded[6:], validate=True), False)
        elif os.name != "nt" and encoded.startswith("fernet:"):
            raw = _fernet(directory, create=False).decrypt(encoded[7:].encode("ascii"))
        else:
            raise ValueError("Unsupported credential")
        original_kind, original_base, token = json.loads(raw)
        if (original_kind, original_base) != (kind, base) or not isinstance(token, str):
            raise ValueError("Credential binding mismatch")
        return token
    except (OSError, ValueError, InvalidToken, TypeError):
        raise CredentialError("已保存的密钥无法读取，请重新填写并保存。") from None
