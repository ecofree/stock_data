"""Non-blocking OS handle locks. Lock files are permanent, never unlinked."""
from __future__ import annotations

import os
from pathlib import Path


class FileLockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.fd: int | None = None

    def __enter__(self):
        if self.fd is not None:
            raise FileLockBusy(f"lock object already entered: {self.path}")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise FileLockBusy(f"lock busy or inaccessible: {self.path}") from exc
        self.fd = fd
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            # Closing the owning descriptor releases the kernel lock even
            # after process death. No PID probing, age-based theft or unlink.
            os.close(fd)
