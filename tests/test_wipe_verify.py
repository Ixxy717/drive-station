import pytest

from drivestation.hw.base import VerifyError
from drivestation.hw.wipe_linux import verify_zeros


def test_verify_zeros_passes_on_zero_file(tmp_path):
    p = tmp_path / "disk.bin"
    p.write_bytes(b"\x00" * (2 * 1024 * 1024))

    def run(argv):
        if argv[0] == "lsblk":
            return 0, str(p.stat().st_size), ""
        return 1, "", ""

    # verify_zeros opens path with os.open — point at our file
    fracs = []
    verify_zeros(str(p), fracs.append, run, samples=4, chunk=64 * 1024)
    assert fracs[-1] == 1.0


def test_verify_zeros_fails_on_nonzero(tmp_path):
    p = tmp_path / "dirty.bin"
    data = bytearray(b"\x00" * (1024 * 1024))
    data[100] = 1
    p.write_bytes(data)

    def run(argv):
        if argv[0] == "lsblk":
            return 0, str(p.stat().st_size), ""
        return 1, "", ""

    with pytest.raises(VerifyError, match="non-zero"):
        verify_zeros(str(p), lambda f: None, run, samples=3, chunk=64 * 1024)
