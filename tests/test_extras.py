"""The [all] extra must cover every optional runtime feature."""

import re
import tomllib
from pathlib import Path

_PYPROJECT = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
_OPTIONAL_DEPS: dict[str, list[str]] = _PYPROJECT["project"]["optional-dependencies"]

# Extras that exist for tooling or that aggregate other extras, not features.
_NOT_A_FEATURE = {"dev", "test", "docs", "all", "all-storage"}


def _referenced_extras(name):
    """Extras named by ``resumable-upload[a,b]`` entries of extra ``name``."""
    assert name in _OPTIONAL_DEPS, f"no [project.optional-dependencies] {name}"
    refs = set()
    for req in _OPTIONAL_DEPS[name]:
        m = re.fullmatch(r"resumable-upload\[([^\]]*)\]", req.replace(" ", ""))
        if m:
            refs.update(m.group(1).split(","))
    return refs


def test_all_extra_is_self_referential():
    # Referencing the extras (rather than restating their pins) is what keeps
    # [all] from drifting when one of them bumps a version.
    assert _referenced_extras("all"), "[all] must reference extras as resumable-upload[...]"


def test_all_extra_covers_every_feature_extra():
    features = [n for n in _OPTIONAL_DEPS if n not in _NOT_A_FEATURE]
    assert features, "no feature extras found"
    missing = [n for n in features if n not in _referenced_extras("all")]
    assert not missing, f"[all] does not cover: {', '.join(missing)}"


# S3 conditional writes (If-Match on PutObject) landed in botocore 1.35.69;
# boto3 1.35.69 is the first release that pins it. S3Storage.update_offset_atomic
# passes IfMatch unconditionally, and botocore raises ParamValidationError —
# not a ClientError, so nothing catches it — when the parameter is unknown.
# An older floor therefore turns every PATCH against S3Storage into a 500.
_BOTO3_IF_MATCH_FLOOR = (1, 35, 69)


def _boto3_pins():
    pins = [
        m.group(1)
        for reqs in _OPTIONAL_DEPS.values()
        for req in reqs
        if (m := re.fullmatch(r"boto3>=([0-9.]+)", req.replace(" ", "")))
    ]
    assert pins, "no boto3 pin found in [project.optional-dependencies]"
    return pins


def test_boto3_floor_supports_conditional_put():
    for pin in _boto3_pins():
        parsed = tuple(int(p) for p in pin.split("."))
        assert parsed >= _BOTO3_IF_MATCH_FLOOR, (
            f"boto3>={pin} predates If-Match on PutObject "
            f"(needs >={'.'.join(str(p) for p in _BOTO3_IF_MATCH_FLOOR)})"
        )
