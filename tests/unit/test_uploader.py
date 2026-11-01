import ftplib
import typing

import pytest

from cheatmaker.console.uploader import Uploader, remote_path, save_to_pc
from cheatmaker.errors import SaveFailed, UploadFailed


def test_save_to_pc_and_overwrite_guard(tmp_path):  # AC-6.5
    p = save_to_pc(tmp_path / "out", "PPSA01234_01.000.json", b"{}")
    assert p.read_bytes() == b"{}"
    with pytest.raises(FileExistsError):
        save_to_pc(tmp_path / "out", "PPSA01234_01.000.json", b"{1}")
    save_to_pc(tmp_path / "out", "PPSA01234_01.000.json", b"{1}", overwrite=True)
    assert p.read_bytes() == b"{1}"


def test_save_to_pc_failure(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SaveFailed):
        save_to_pc(blocker, "a.json", b"{}")  # parent is a file


class FakeFTP:
    files: typing.ClassVar[dict[str, bytes]] = {}
    dirs: typing.ClassVar[set[str]] = {"/", "/data"}
    fail_connect = False

    def __init__(self):
        self.cwd_path = "/"

    def connect(self, host, port, timeout):
        if FakeFTP.fail_connect:
            raise OSError("refused")

    def login(self):
        pass

    def close(self):
        pass

    def cwd(self, p):
        if p not in FakeFTP.dirs:
            raise ftplib.error_perm("550")
        self.cwd_path = p

    def mkd(self, p):
        FakeFTP.dirs.add(p)

    def nlst(self, p):
        if p not in FakeFTP.dirs:
            raise ftplib.error_perm("550")
        return [k for k in FakeFTP.files if k.startswith(p + "/")]

    def storbinary(self, cmd, fp):
        FakeFTP.files[self.cwd_path + "/" + cmd.split(" ", 1)[1]] = fp.read()


@pytest.fixture(autouse=True)
def _reset():
    FakeFTP.files, FakeFTP.dirs, FakeFTP.fail_connect = {}, {"/", "/data"}, False


def test_upload_creates_folders_and_stores():  # AC-6.3
    up = Uploader("10.0.0.5", ftp_factory=FakeFTP)
    assert not up.exists("json", "PPSA01234_01.000.json")
    path = up.upload("json", "PPSA01234_01.000.json", b"{}")
    assert path == "/data/cheatrunner/cheats/json/PPSA01234_01.000.json"
    assert FakeFTP.files[path] == b"{}"
    assert up.exists("json", "PPSA01234_01.000.json")
    assert remote_path("shn", "a.shn") == "/data/cheatrunner/cheats/shn/a.shn"


def test_upload_unreachable():
    FakeFTP.fail_connect = True
    with pytest.raises(UploadFailed):
        Uploader("10.0.0.5", ftp_factory=FakeFTP).upload("json", "a.json", b"{}")
