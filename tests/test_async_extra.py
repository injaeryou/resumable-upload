"""The [async] extra declares httpx; core import must not require it."""

import subprocess
import sys
import tomllib
from pathlib import Path

_PYPROJECT = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())


def test_async_extra_declares_httpx():
    extras = _PYPROJECT["project"]["optional-dependencies"]
    assert "async" in extras, "no [project.optional-dependencies] async"
    assert any(req.startswith("httpx") for req in extras["async"])


def test_core_import_does_not_require_httpx():
    # Importing the package must succeed even if httpx is hidden.
    code = (
        "import sys; sys.modules['httpx'] = None; "
        "import resumable_upload; print(resumable_upload.__name__)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "resumable_upload" in out.stdout
