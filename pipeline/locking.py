"""Small cross-platform advisory lock for scheduled database maintenance."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class LockUnavailableError(RuntimeError):
    """Raised when another process already owns a requested lock."""


class InterProcessLock:
    """Hold a non-blocking OS lock for the lifetime of this context manager.

    The sidecar file deliberately remains after release.  Removing an advisory
    lock file creates an inode/path race in which two processes can each lock a
    different file.  The kernel releases the actual lock automatically if the
    owner exits or crashes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError(f"Lock is already held by this object: {self.path}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            # Windows byte-range locks require the byte to exist first.
            if os.fstat(descriptor).st_size == 0:
                handle.write(b"\0")
            handle.seek(0)
            _try_lock(handle)
        except OSError as exc:
            owner = _read_owner(handle)
            handle.close()
            detail = f"; last owner metadata: {owner}" if owner else ""
            raise LockUnavailableError(
                f"Another weekly refresh already holds {self.path}{detail}"
            ) from exc
        except BaseException:
            handle.close()
            raise

        try:
            metadata = (
                f"pid={os.getpid()} acquired_at="
                f"{datetime.now(timezone.utc).isoformat()}\n"
            ).encode("utf-8")
            handle.seek(1)
            handle.write(metadata)
            handle.truncate()
        except BaseException:
            try:
                handle.seek(0)
                _unlock(handle)
            finally:
                handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> InterProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _read_owner(handle: BinaryIO) -> str:
    try:
        handle.seek(1)
        return handle.read(512).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


if os.name == "nt":
    import msvcrt

    def _try_lock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["InterProcessLock", "LockUnavailableError"]
