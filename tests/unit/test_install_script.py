import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


def test_missing_homebrew_is_a_clear_error(tmp_path):
    # a PATH with the basic tools only: no brew, no uv
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("uname", "dirname", "printf", "cat"):
        src = next(p for d in ("/usr/bin", "/bin") if (p := Path(d) / tool).exists())
        (bin_ / tool).symlink_to(src)
    r = subprocess.run(["/bin/bash", str(SCRIPT)], env={"PATH": str(bin_), "HOME": str(tmp_path)}, capture_output=True, text=True)
    assert r.returncode == 1
    assert "Homebrew is required" in r.stderr and "https://brew.sh" in r.stderr
