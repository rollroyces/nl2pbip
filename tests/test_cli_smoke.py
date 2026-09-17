"""CLI smoke tests.

These tests boot the CLI as a real subprocess (``python -m
nl2pbip.cli``) and assert that ``--help`` exits 0 and the
expected flags are surfaced. The intent is to catch a
regression where the argparse setup silently drops a flag
(or the subcommand parser breaks entirely) — neither would
be caught by the in-process unit tests in
``test_cli``/``test_orchestrator`` etc.

Design notes:

* Use ``sys.executable`` so the test works under CI (no
  ``.venv`` mounted) and locally (``make test`` invokes the
  venv's Python).
* ``capture_output=True`` keeps pytest's stdout clean.
* The expected-flag list mirrors the ``_build_generate_parser``
  definition in ``nl2pbip/cli.py``. If you add a new flag
  there, add it here too — the test fails with a precise
  "missing flag" message so the regression is obvious.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


# Flags the ``generate`` subcommand must expose. Order is not
# significant — argparse prints flags in declaration order, but
# we don't assert that here.
EXPECTED_GENERATE_FLAGS = (
    "--prompt",
    "--provider",
    "--model",
    "--base-url",
    "--api-key",
    "--workspace",
    "--output",
    "--project-name",
    "--dax-library",
    "--export",
    "--export-output",
    "--api-version",
)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python -m nl2pbip.cli <args>`` and return the
    completed process. Uses ``sys.executable`` so the test
    follows the active interpreter (CI's system Python, the
    developer's ``.venv``, or whatever else pytest was
    launched with).
    """
    return subprocess.run(
        [sys.executable, "-m", "nl2pbip.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_cli_generate_help_exits_zero() -> None:
    """``python -m nl2pbip.cli generate --help`` must exit 0
    and emit the usage banner + every expected flag. A non-
    zero exit here means the parser is broken (typo, missing
    import, circular import) — the smoke test will catch
    that before any real user invocation does.
    """
    result = _run_cli("generate", "--help")
    assert result.returncode == 0, (
        f"generate --help exited {result.returncode} "
        f"(expected 0).\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    # argparse writes the usage banner + per-flag help to
    # stdout, not stderr. Make sure we actually got help text
    # rather than an ImportError or similar traceback.
    assert "usage:" in result.stdout, (
        f"generate --help stdout is missing the usage banner. "
        f"Got:\n{result.stdout}"
    )
    for flag in EXPECTED_GENERATE_FLAGS:
        assert flag in result.stdout, (
            f"generate --help is missing the {flag!r} flag. "
            f"Did someone remove it from "
            f"_build_generate_parser?\nGot:\n{result.stdout}"
        )


def test_cli_root_help_exits_zero() -> None:
    """``python -m nl2pbip.cli --help`` must also exit 0.

    The CLI doesn't expose subcommand-routing on the root
    parser (the ``generate`` / ``export`` choice is a string
    check in ``parse_args``), so ``--help`` at the root just
    delegates to ``generate``'s parser. We assert the same
    flag coverage so a regression at the top level is
    surfaced by the same test.
    """
    result = _run_cli("--help")
    assert result.returncode == 0, (
        f"cli --help exited {result.returncode} (expected 0)."
        f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "usage:" in result.stdout, (
        f"cli --help stdout is missing the usage banner. "
        f"Got:\n{result.stdout}"
    )
    # Same flag coverage as the subcommand help — the root
    # parser is just the generate parser with subcommand
    # routing layered on top.
    for flag in EXPECTED_GENERATE_FLAGS:
        assert flag in result.stdout, (
            f"cli --help is missing the {flag!r} flag. "
            f"Got:\n{result.stdout}"
        )


@pytest.mark.parametrize("flag", EXPECTED_GENERATE_FLAGS)
def test_cli_generate_help_lists_each_flag(flag: str) -> None:
    """Parametrised coverage: every expected flag must appear
    in ``generate --help`` output. A regression that drops a
    single flag is caught with a precise message.
    """
    result = _run_cli("generate", "--help")
    assert result.returncode == 0
    assert flag in result.stdout, (
        f"generate --help is missing the {flag!r} flag"
    )


def test_cli_export_help_exits_zero() -> None:
    """``python -m nl2pbip.cli export --help`` should also
    exit 0 — the subcommand dispatcher in ``parse_args``
    routes the ``export`` keyword to ``_build_export_parser``
    whose own ``--help`` is just as important to keep
    regression-free.
    """
    result = _run_cli("export", "--help")
    assert result.returncode == 0, (
        f"export --help exited {result.returncode} (expected 0)."
        f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "usage:" in result.stdout
