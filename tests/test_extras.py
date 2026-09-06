"""The [all] extra must cover every optional runtime feature."""

import re
from pathlib import Path

_PYPROJECT = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()

# Text-based (no tomllib — must pass on the 3.9/3.10 tox envs too).
_OPTIONAL_DEPS = re.search(r"(?ms)^\[project\.optional-dependencies\]\n(.*?)^\[", _PYPROJECT).group(
    1
)

# Extras that exist for tooling or that aggregate other extras, not features.
_NOT_A_FEATURE = {"dev", "test", "docs", "all", "all-storage"}


def _extra(name):
    m = re.search(rf"(?ms)^{re.escape(name)}\s*=\s*\[(.*?)^\]", _OPTIONAL_DEPS)
    assert m, f"no [project.optional-dependencies] {name} = [...] block"
    return m.group(1)


def test_all_extra_is_self_referential():
    # Referencing the extras (rather than restating their pins) is what keeps
    # [all] from drifting when one of them bumps a version.
    assert "resumable-upload[" in _extra("all")


def test_all_extra_covers_every_feature_extra():
    names = re.findall(r"(?m)^([a-z][a-z0-9-]*)\s*=\s*\[", _OPTIONAL_DEPS)
    features = [n for n in names if n not in _NOT_A_FEATURE]
    assert features, "no feature extras found — regex out of date?"
    referenced = _extra("all")
    missing = [n for n in features if not re.search(rf"[\[,]{n}[,\]]", referenced)]
    assert not missing, f"[all] does not cover: {', '.join(missing)}"
