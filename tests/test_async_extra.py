"""The [async] extra declares httpx; core import must not require it."""

import re
import subprocess
import sys
from pathlib import Path

_PYPROJECT = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()


def test_async_extra_declares_httpx():
    # Text-based (no tomllib — must pass on the 3.9/3.10 tox envs too).
    m = re.search(r"(?ms)^async\s*=\s*\[(.*?)\]", _PYPROJECT)
    assert m, "no [project.optional-dependencies] async = [...] block"
    assert "httpx" in m.group(1)


def test_core_import_does_not_require_httpx():
    # Importing the package must succeed even if httpx is hidden.
    code = (
        "import sys; sys.modules['httpx'] = None; "
        "import resumable_upload; print(resumable_upload.__name__)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "resumable_upload" in out.stdout
