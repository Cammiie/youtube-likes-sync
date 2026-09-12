from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


class SyncError(Exception):
    """Only fixed, credential-free descriptions belong in this exception."""
    def __init__(self, code: str, delay: int = 300):
        self.code, self.delay = code, delay
        super().__init__(code)


def data_dir() -> Path:
    # Codex's packaged Windows process can virtualize LocalAppData writes. Pin
    # the actual directory so a normal Task Scheduler process sees the same DB.
    from .runtime import app_dir
    location = app_dir() / 'runtime-path.json'
    if location.exists():
        configured = Path(json.loads(location.read_text(encoding='utf-8'))['data_dir'])
        if configured.is_absolute():
            return configured
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "YouTubeLikesSync"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def config(root: Path) -> dict:
    defaults = {"output": str(Path.home() / "Music" / "YouTube Likes"),
                "download_engine": "antra_tidal",
                "browser_launch_allowed": False,
                "browser_window_mode": "minimized",
                "browser_downloads_ready": True,
                "auto_api_renewal": False,
                "api_renewal_lead_seconds": 600,
                "api_renewal_retry_seconds": 1800,
                "allow_encrypted_lossless": False,
                "max_download_bytes": 2 * 1024**3, "max_job_seconds": 1200,
                "max_jobs_per_run": 25}
    path = root / "config.json"
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    return defaults


def dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise SyncError("windows_dpapi_required")

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, dest = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), "YouTubeLikesSync", None, None, None, 1, ctypes.byref(dest))
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise SyncError("credential_decryption_failed" if decrypt else "credential_encryption_failed")
    try:
        return ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        kernel.LocalFree(dest.pbData)


def save_auth(root: Path, headers: dict) -> None:
    atomic_write(root / "auth.dpapi", dpapi(json.dumps(headers).encode()))


def load_auth(root: Path) -> dict:
    try:
        return json.loads(dpapi((root / "auth.dpapi").read_bytes(), decrypt=True))
    except FileNotFoundError:
        raise SyncError("setup_required") from None


class RunLock:
    """Kernel-released lock; a crashed process cannot leave a stale ownership flag."""
    def __init__(self, root: Path):
        self.path = root / "worker.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SyncError("already_running") from None
        return self

    def __exit__(self, *args):
        self.file.close()
