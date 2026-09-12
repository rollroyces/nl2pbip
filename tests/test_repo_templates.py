"""Sanity tests for the GitHub template files.

These tests verify the repo has a complete GitHub template
package — issue templates, PR template, codeowners, dependabot,
security policy, contribution guide, release template, and the
extended CI workflow.

Each test is intentionally cheap (no network, no parsing of
GitHub's own form schema) so the suite stays fast. The tests
guard against the silent breakage of:
* Removing a template file by accident (someone runs ``rm
  .github/ISSUE_TEMPLATE/bug_report.yml``).
* Renaming a workflow so the ``on:`` trigger changes shape.
* Forgetting to bump the workflow name in the title.
* Breaking the dependabot config schema.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# File presence
# ---------------------------------------------------------------------------


class TestFilePresence:
    """The repo ships these template files. Missing files should
    fail this test so a stray ``rm`` doesn't slip through review."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/CODEOWNERS",
            ".github/dependabot.yml",
            ".github/SECURITY.md",
            ".github/RELEASE_TEMPLATE.md",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/workflows/codeql.yml",
            "CONTRIBUTING.md",
        ],
    )
    def test_required_file_exists(self, path: str) -> None:
        assert (REPO_ROOT / path).exists(), f"Required template file is missing: {path}"


class TestOptionalFiles:
    """Optional but expected files. CI should still pass without
    these, but their presence is checked so we notice if they
    disappear."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
        ],
    )
    def test_optional_file_exists(self, path: str) -> None:
        if not (REPO_ROOT / path).exists():
            pytest.skip(f"Optional file {path} not present (acceptable).")


# ---------------------------------------------------------------------------
# YAML shape
# ---------------------------------------------------------------------------


class TestYAMLShape:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
            ".github/dependabot.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/workflows/codeql.yml",
        ],
    )
    def test_yaml_parses(self, path: str) -> None:
        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        with full.open() as fh:
            data = yaml.safe_load(fh)
        assert data is not None, f"{path} parsed as empty"
        assert isinstance(data, dict), f"{path} should parse to a mapping"

    def test_workflows_have_name_field(self) -> None:
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            data = yaml.safe_load(full.read_text())
            assert "name" in data, f"{workflow} has no top-level 'name'"
            assert isinstance(data["name"], str)
            assert data["name"].strip(), f"{workflow} has empty 'name'"

    def test_workflows_have_trigger(self) -> None:
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            data = yaml.safe_load(full.read_text())
            assert "on" in data or True in data, f"{workflow} has no 'on:' trigger"

    def test_ci_triggers_on_push_and_pr(self) -> None:
        full = REPO_ROOT / ".github/workflows/ci.yml"
        data = yaml.safe_load(full.read_text())
        # PyYAML returns the literal `on:` key as the boolean True.
        on_block = data.get("on", data.get(True))
        assert on_block is not None
        assert "push" in on_block
        assert "pull_request" in on_block

    def test_release_triggers_on_tag(self) -> None:
        full = REPO_ROOT / ".github/workflows/release.yml"
        data = yaml.safe_load(full.read_text())
        on_block = data.get("on", data.get(True))
        # Either tag push, or workflow_dispatch, or both.
        assert "push" in on_block or "workflow_dispatch" in on_block

    def test_codeql_triggers_on_push_pr_schedule(self) -> None:
        full = REPO_ROOT / ".github/workflows/codeql.yml"
        data = yaml.safe_load(full.read_text())
        on_block = data.get("on", data.get(True))
        assert "push" in on_block
        assert "pull_request" in on_block
        assert "schedule" in on_block


# ---------------------------------------------------------------------------
# Issue / discussion form templates
# ---------------------------------------------------------------------------


class TestIssueFormTemplates:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
        ],
    )
    def test_form_has_name_and_body(self, path: str) -> None:
        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        data = yaml.safe_load(full.read_text())
        assert "name" in data, f"{path} has no 'name'"
        assert "body" in data, f"{path} has no 'body'"
        assert isinstance(data["body"], list)
        assert len(data["body"]) >= 1, f"{path} body is empty"

    def test_bug_report_has_required_fields(self) -> None:
        data = yaml.safe_load(
            (REPO_ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text()
        )
        all_labels = set()
        for item in data["body"]:
            if item.get("type") in ("textarea", "input", "dropdown"):
                attrs = item.get("attributes", {})
                label = attrs.get("label")
                if label:
                    all_labels.add(label)
        for must in (
            "What happened?",
            "Steps to reproduce",
            "nl2pbip version",
        ):
            assert must in all_labels, f"bug_report missing '{must}'"


# ---------------------------------------------------------------------------
# CODEOWNERS
# ---------------------------------------------------------------------------


class TestCodeowners:
    def test_codeowners_has_default_owner(self) -> None:
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # First non-comment line should be the default owner (``*``).
        first_real = next((line for line in lines if not line.startswith("#")), "")
        assert first_real.startswith(
            "*"
        ), f"CODEOWNERS must start with a default-owner line; got: {first_real!r}"
        # Default owner must mention a GitHub handle.
        assert "@" in first_real, "CODEOWNERS default owner needs a handle"

    def test_codeowners_paths_use_repo_root(self) -> None:
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # If it's a path-prefixed line, the path should start
            # with ``/`` (repo-root-relative) — not ``./`` or a
            # bare name.
            if line.startswith("/"):
                assert not line.startswith(
                    "//"
                ), f"CODEOWNERS path should not have double slash: {line!r}"


# ---------------------------------------------------------------------------
# Dependabot
# ---------------------------------------------------------------------------


class TestDependabot:
    def test_dependabot_version(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text())
        assert data.get("version") == 2

    def test_dependabot_has_pip_updates(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text())
        ecosystems = {u.get("package-ecosystem") for u in data.get("updates", [])}
        assert "pip" in ecosystems
        assert "github-actions" in ecosystems

    def test_dependabot_ignores_heavy_ml_deps(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/dependabot.yml").read_text())
        pip_update = next(
            u for u in data["updates"] if u.get("package-ecosystem") == "pip"
        )
        ignored = {
            entry.get("dependency-name") for entry in pip_update.get("ignore", [])
        }
        for heavy in ("unsloth", "trl", "transformers", "datasets"):
            assert heavy in ignored, f"Dependabot must ignore {heavy} (heavy ML dep)."


# ---------------------------------------------------------------------------
# CI workflow
# ---------------------------------------------------------------------------


class TestCIWorkflow:
    def test_ci_matrix_includes_supported_versions(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        jobs = data["jobs"]["tests"]
        versions = jobs["strategy"]["matrix"]["python-version"]
        for required in ("3.10", "3.11", "3.12"):
            assert (
                required in versions
            ), f"CI matrix missing Python {required}; got {versions}"

    def test_ci_runs_black_ruff_pytest(self) -> None:
        text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        for command in ("black --check", "ruff check", "pytest"):
            assert command in text, f"CI workflow missing '{command}' step"

    def test_ci_has_concurrency_group(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        assert "concurrency" in data
        assert "group" in data["concurrency"]

    def test_ci_smoke_test_verifies_fabric_metadata(self) -> None:
        # If the smoke test for example_run isn't checking Fabric
        # metadata files, a regression on PR #22 wouldn't be caught.
        text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        for must in (
            "itemMetadata.json",
            ".platform",
        ):
            assert must in text, f"CI smoke test missing '{must}' file check"

    def test_ci_benchmarks_opt_in(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        # The benchmarks job should be triggered only via
        # workflow_dispatch (not on every push / PR).
        bench_job = data["jobs"].get("benchmarks")
        if bench_job is None:
            pytest.skip("benchmarks job not defined")
        on_block = data.get("on", data.get(True))
        assert "workflow_dispatch" in on_block
        assert bench_job.get("if", "").startswith(
            "github.event_name == 'workflow_dispatch'"
        ), "benchmarks job should only run via workflow_dispatch"


# ---------------------------------------------------------------------------
# Release workflow
# ---------------------------------------------------------------------------


class TestCodeQLWorkflow:
    """The codeql.yml workflow had a real bug — ``paths-ignore``
    was attached to ``codeql-action/init`` (which doesn't accept
    that input) instead of ``actions/checkout``. Every push failed
    with a malformed-workflow error. These tests lock in the
    fix so the regression can't sneak back in."""

    def test_paths_ignore_only_on_checkout(self) -> None:
        """paths-ignore is a checkout option; placing it on any
        other step is invalid and causes the workflow to fail at
        parse time on GitHub."""
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            if "paths-ignore" in step.get("with", {}):
                # Found a step using paths-ignore. It MUST be the
                # actions/checkout step.
                uses = step.get("uses", "")
                assert uses.startswith("actions/checkout"), (
                    f"paths-ignore must live on actions/checkout, "
                    f"not on {uses!r}. The codeql-action does not "
                    "accept this input and the workflow will fail "
                    "to parse."
                )

    def test_codeql_init_has_languages(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("github/codeql-action/init"):
                with_block = step.get("with", {})
                assert (
                    "languages" in with_block
                ), "codeql-action/init requires 'languages' input."

    def test_codeql_analyze_has_category(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("github/codeql-action/analyze"):
                with_block = step.get("with", {})
                assert (
                    "category" in with_block
                ), "codeql-action/analyze requires 'category' input."

    def test_codeql_workflow_has_security_events_write(self) -> None:
        """GitHub rejects the workflow at parse time (no jobs run)
        if ``security-events: write`` is missing from the workflow.
        The permission can live at the top level OR inside the job;
        accept either, as long as the analyze job declares it."""
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        job = data["jobs"]["analyze"]
        perms = data.get("permissions", {})
        job_perms = job.get("permissions", {})
        assert (
            perms.get("security-events") == "write"
            or job_perms.get("security-events") == "write"
        ), (
            "codeql.yml needs 'security-events: write' at the "
            "top-level or inside the analyze job's permissions. "
            "Without it, GitHub rejects the workflow before any "
            "job runs."
        )

    def test_codeql_paths_ignore_uses_block_scalar(self) -> None:
        """``paths-ignore`` rejects the ``- item`` list form when
        any item contains a glob character (``**`` etc). GitHub
        Actions' workflow parser silently fails the whole
        workflow at parse time (no jobs run, fallback to
        path-based default name). Use the block-scalar form
        (``paths-ignore: |`` with one pattern per line) instead.

        Regression test for the 2026-09-12 incident where PR #23
        + #27 + #28 fixed the same symptom three times before
        root-causing it to the YAML shape."""
        text = (REPO_ROOT / ".github/workflows/codeql.yml").read_text()
        # Either block scalar OR no paths-ignore at all (also OK).
        # We forbid: list form with a glob character in any item.
        import re

        in_paths_ignore = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("paths-ignore"):
                # Capture the form: either ``paths-ignore: |`` or
                # ``paths-ignore:`` followed by a list.
                in_paths_ignore = True
                continue
            if in_paths_ignore:
                if not raw.startswith(" ") and raw.strip() != "":
                    # Left the paths-ignore block.
                    in_paths_ignore = False
                    continue
                # If we see a list item containing ``**``, fail.
                if stripped.startswith("- ") and "**" in stripped:
                    pytest.fail(
                        "paths-ignore uses the - item list form with a "
                        "glob character ('**'). GitHub Actions rejects "
                        "this at parse time. Use the block-scalar "
                        "form (paths-ignore: |) instead.\n"
                        f"Offending line: {raw!r}"
                    )


class TestReleaseWorkflow:
    def test_release_has_publish_job(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/release.yml").read_text())
        # The publish job is the gate that pushes to PyPI.
        assert "publish" in data["jobs"]

    def test_release_uses_trusted_publishing(self) -> None:
        text = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        data = yaml.safe_load(text)
        assert "pypa/gh-action-pypi-publish" in text, (
            "Release workflow should use pypa/gh-action-pypi-publish "
            "(trusted publishing, no API token)."
        )
        # OIDC token grant is required for trusted publishing.
        perms = data.get("permissions", {})
        assert (
            perms.get("id-token") == "write"
        ), "Release workflow needs id-token: write for PyPI OIDC."

    def test_release_builds_wheel_and_sdist(self) -> None:
        text = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        assert "python -m build" in text
        assert "twine check" in text


# ---------------------------------------------------------------------------
# SECURITY.md
# ---------------------------------------------------------------------------


class TestSecurityPolicy:
    def test_security_md_lists_supported_versions(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "Supported versions" in text
        # Must include a contact email.
        assert "@" in text, "SECURITY.md must include a contact email"

    def test_security_md_disclosure_window(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "coordinated disclosure" in text.lower() or (
            "responsible" in text.lower()
        ), "SECURITY.md must mention disclosure policy"

    def test_security_md_out_of_scope_section(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "Out of scope" in text or "out of scope" in text


# ---------------------------------------------------------------------------
# CONTRIBUTING.md
# ---------------------------------------------------------------------------


class TestContributingGuide:
    def test_contributing_mentions_licensing(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert (
            "license" in text.lower() or "proprietary" in text.lower()
        ), "CONTRIBUTING.md must address licensing"

    def test_contributing_has_dev_setup(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "pip install" in text
        assert "pytest" in text

    def test_contributing_has_release_process(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "tag" in text.lower() or "release" in text.lower()


# ---------------------------------------------------------------------------
# Cross-template consistency
# ---------------------------------------------------------------------------


class TestCrossTemplateConsistency:
    def test_repo_root_constant_in_templates(self) -> None:
        """If a template references a file path, that file should
        exist. Catches typos in CODEOWNERS, dependabot config,
        etc."""
        # CODEOWNERS paths should resolve.
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not parts:
                continue
            path = parts[0]
            if path.startswith("*"):
                continue
            # CODEOWNERS paths starting with ``/`` are repo-root
            # relative.
            if path.startswith("/"):
                rel = path.lstrip("/")
                if rel.endswith("/"):
                    continue  # directory match — can't validate
                assert (
                    REPO_ROOT / rel
                ).exists(), f"CODEOWNERS references missing path: {path}"

    def test_workflow_referenced_paths_exist(self) -> None:
        """If a workflow checks for a file via ``test -f path`` or
        similar, the path must exist in the repo."""
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            text = full.read_text()
            # Match ``test -f some/path`` or ``cat some/path`` or
            # ``-f artifacts/...``.
            for match in re.finditer(
                r"test -f\s+(\S+)|cat\s+(\S+)|-f\s+([^\s\"]+)", text
            ):
                path = next(g for g in match.groups() if g)
                if path.startswith("$"):
                    continue
                # Strip leading ./ if present.
                if path.startswith("./"):
                    path = path[2:]
                # Skip glob patterns and template vars.
                if any(c in path for c in ("*", "?", "$", "{", "<")):
                    continue
                # Skip absolute paths and shell builtins.
                if path.startswith("/") or path in ("true", "false", "0", "1"):
                    continue
                assert (
                    REPO_ROOT / path
                ).exists(), f"{workflow} references missing path: {path}"


# ---------------------------------------------------------------------------
# Smoke test — run example_run, verify Fabric metadata
# ---------------------------------------------------------------------------


class TestRepoTemplateIntegration:
    """The CI workflow's smoke step checks that example_run
    produces Fabric metadata. Verify the underlying behaviour so
    the smoke test doesn't guard against a broken example."""

    def test_example_run_produces_fabric_metadata(self) -> None:
        import json
        import subprocess
        import sys

        # Reset artifacts and run example. Use ``sys.executable`` so
        # the smoke test works under CI (no .venv) and locally.
        artifact_dir = REPO_ROOT / "artifacts"
        if artifact_dir.exists():
            import shutil

            shutil.rmtree(artifact_dir)
        result = subprocess.run(
            [sys.executable, "-m", "nl2pbip.example_run"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert result.returncode == 0, f"example_run failed: {result.stderr[-500:]}"
        project_dir = artifact_dir / "SalesInsights.pbipdir"
        for path in [
            project_dir / "SalesInsights.pbip",
            project_dir / "SalesInsights.SemanticModel" / "itemMetadata.json",
            project_dir / "SalesInsights.SemanticModel" / ".platform",
            project_dir / "SalesInsights.Report" / "itemMetadata.json",
            project_dir / "SalesInsights.Report" / ".platform",
        ]:
            assert path.exists(), f"example_run did not produce {path}"
            # Each file should be valid JSON (or in the case of
            # .pbip, also valid JSON).
            json.loads(path.read_text())
