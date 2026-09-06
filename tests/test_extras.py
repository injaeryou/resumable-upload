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


# S3 conditional writes (If-Match on PutObject) landed in botocore 1.35.69;
# boto3 1.35.69 is the first release that pins it. S3Storage.update_offset_atomic
# passes IfMatch unconditionally, and botocore raises ParamValidationError —
# not a ClientError, so nothing catches it — when the parameter is unknown.
# An older floor therefore turns every PATCH against S3Storage into a 500.
_BOTO3_IF_MATCH_FLOOR = (1, 35, 69)


def _boto3_pins():
    pins = re.findall(r'"boto3>=([0-9.]+)"', _OPTIONAL_DEPS)
    assert pins, "no boto3 pin found in [project.optional-dependencies]"
    return pins


def test_boto3_floor_supports_conditional_put():
    for pin in _boto3_pins():
        parsed = tuple(int(p) for p in pin.split("."))
        assert parsed >= _BOTO3_IF_MATCH_FLOOR, (
            f"boto3>={pin} predates If-Match on PutObject "
            f"(needs >={'.'.join(str(p) for p in _BOTO3_IF_MATCH_FLOOR)})"
        )
