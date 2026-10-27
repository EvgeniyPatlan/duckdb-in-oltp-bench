import subprocess
import sys


def test_cli_version():
    out = subprocess.run(
        [sys.executable, "-m", "dbench.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "0.1.0"


def test_console_script_version():
    out = subprocess.run(["dbench", "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0.1.0"
