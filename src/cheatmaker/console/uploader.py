"""Save cheat files (AC-6.1/6.2/6.5) and send them to the PS5 (AC-6.3).

Upload goes to the ftpsrv payload (port 2121) with the stdlib ftplib; saving on
the PC is always available as the fallback.
"""

from __future__ import annotations

import ftplib
import logging
import posixpath
from pathlib import Path

from cheatmaker.errors import SaveFailed, UploadFailed

log = logging.getLogger(__name__)

REMOTE_DIRS = {
    "json": "/data/cheatrunner/cheats/json",
    "shn": "/data/cheatrunner/cheats/shn",
}
FTP_TIMEOUT = 10


def remote_path(fmt: str, file_name: str) -> str:
    return posixpath.join(REMOTE_DIRS[fmt], file_name)


def save_to_pc(folder: Path, file_name: str, data: bytes, overwrite: bool = False) -> Path:
    """Write the file. Raises FileExistsError if it exists and ``overwrite`` is False,
    so the UI can ask first (AC-6.5)."""
    target = folder / file_name
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    except OSError as exc:
        raise SaveFailed(f"can't write {target}: {exc}") from exc
    log.info("action=file_save path=%s bytes=%d", target, len(data))
    return target


class Uploader:
    def __init__(self, host: str, port: int = 2121, ftp_factory=ftplib.FTP) -> None:
        self.host, self.port = host, port
        self._factory = ftp_factory

    def _open(self) -> ftplib.FTP:
        ftp = self._factory()
        try:
            ftp.connect(self.host, self.port, timeout=FTP_TIMEOUT)
            ftp.login()  # ftpsrv accepts anonymous
        except (OSError, ftplib.Error) as exc:
            try:
                ftp.close()
            finally:
                raise UploadFailed(f"can't reach ftpsrv at {self.host}:{self.port}: {exc}") from exc
        return ftp

    @staticmethod
    def _ensure_dir(ftp: ftplib.FTP, path: str) -> None:
        current = ""
        for part in path.strip("/").split("/"):
            current += "/" + part
            try:
                ftp.cwd(current)
            except ftplib.error_perm:
                ftp.mkd(current)
                ftp.cwd(current)

    def exists(self, fmt: str, file_name: str) -> bool:
        ftp = self._open()
        try:
            try:
                names = ftp.nlst(REMOTE_DIRS[fmt])
            except ftplib.error_perm:
                return False  # folder not there yet
            return any(posixpath.basename(n) == file_name for n in names)
        except (OSError, ftplib.Error) as exc:
            raise UploadFailed(f"can't list {REMOTE_DIRS[fmt]}: {exc}") from exc
        finally:
            ftp.close()

    def upload(self, fmt: str, file_name: str, data: bytes) -> str:
        import io

        ftp = self._open()
        try:
            self._ensure_dir(ftp, REMOTE_DIRS[fmt])
            ftp.storbinary(f"STOR {file_name}", io.BytesIO(data))
        except (OSError, ftplib.Error) as exc:
            raise UploadFailed(f"upload of {file_name} failed: {exc}") from exc
        finally:
            ftp.close()
        path = remote_path(fmt, file_name)
        log.info("action=upload path=%s bytes=%d", path, len(data))
        return path
