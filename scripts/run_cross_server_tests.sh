#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Wrapper around the cross-server test invocation. Records which
# test files ran + the dependencies that produced the result, then
# writes a self-describing ``cross-server-validation-result.json``
# artifact so the GitHub Actions upload has a documented lineage.
#
# Why a wrapper rather than inline YAML:
#   - GitHub's workflow YAML rejects large indented Python heredocs
#     (the validator chokes on the ``import`` keyword); bash keeps
#     the logic maintainable.
#   - The same wrapper can be reused for local re-runs (e.g.
#     ``NL2PBIP_RUN_CROSS_SERVER_TESTS=1 bash
#     scripts/run_cross_server_tests.sh``).
#
# Exits with the pytest exit code so the Actions step still gates.
# ---------------------------------------------------------------------

set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

# Run pytest with a JSON report so we can embed per-test pass/fail
# counts in the wrapper output. ``pytest-json-report`` is bundled
# in the ``[dev]`` extra; if it's missing the test module's
# ``--json-report`` flag will surface a clear error and we'll fall
# back to text-only output.
set +e
python -m pytest tests/test_cross_server_validation.py -v \
    --json-report --json-report-file=pytest-report.json \
    > pytest.log 2>&1
EXIT=$?
set -e

# Write the self-describing result JSON. We use Python (not jq) so
# the wrapper has zero non-stdlib runtime deps. The JSON includes:
#   - which test files ran (so a reviewer can grep for them)
#   - which dependencies the run actually used (resolves the v1.6.1
#     review note about "artifact lacks the deps that produced it")
#   - the platform / Python / npm metadata a future debugger needs
python <<'PY' > cross-server-validation-result.json
import datetime as _dt
import json as _json
import os as _os
import platform as _platform
import sys as _sys

result = {
    "workflow": "cross-server",
    "run_id": _os.environ.get("GITHUB_RUN_ID", "local"),
    "pr_number": _os.environ.get("GITHUB_REF_NAME", "local"),
    "platform": _platform.platform(),
    "python_version": _platform.python_version(),
    "produced_at_utc": _dt.datetime.utcnow().isoformat() + "Z",
    "test_files_invoked": [
        "tests/test_cross_server_validation.py",
    ],
    "test_runner": "pytest",
    "dependencies": {
        "package_manager": "npm",
        "npm_package": "@microsoft/powerbi-modeling-mcp@0.5.0-beta.13",
        "python_extras": "[dev,mcp]",
        "install_script": "scripts/install_powerbi_modeling_mcp.sh",
        "wrapper_script": "scripts/run_cross_server_tests.sh",
        "node_version": "20",
        "python_version": "3.12",
        "platform_binary": (
            "node_modules/@microsoft/powerbi-modeling-mcp-{arch}"
            "/dist/powerbi-modeling-mcp"
        ),
    },
}
try:
    with open("pytest-report.json") as fh:
        result["pytest_report"] = _json.load(fh)
except FileNotFoundError:
    pass
_json.dump(result, _sys.stdout, indent=2)
PY

echo "Pytest exit: $EXIT"
exit "$EXIT"