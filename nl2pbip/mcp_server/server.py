"""MCP server implementation for nl2pbip.

See :mod:`nl2pbip.mcp_server` for the design constraints; this module
holds the implementation.

The four MCP tools
------------------

``generate_report(prompt, workspace, output_dir?, project_name?,
                  max_cost_usd?, plan_chunk_size?)``
    Full pipeline: prompt -> TMDL + PBIR -> packaged .pbip folder.
    Identical semantics to ``nl2pbip generate --prompt ...``.

``validate_pbip(pbip_path)``
    Open an existing .pbip folder and run :class:`PBIRValidator` over
    every page + visual, returning a structured report (errors,
    warnings, validated page/visual counts). No LLM call.

``inspect_dataset(source, max_rows?, redact_distinct_values?)``
    Profile one data source (CSV / JSON / JSONL / Parquet / in-memory
    records). No LLM call. PII-safe by default
    (``redact_distinct_values=False``; flip to ``True`` when sending
    the profile to a hosted LLM with strict data-residency rules).

``version()``
    Return the running :data:`nl2pbip.__version__` and the prompt
    version constant :data:`REPORT_GENERATION_PROMPT_VERSION` so a
    client can confirm it is talking to the expected server build.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Sequence, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP

from nl2pbip import __version__ as _NL2PBIP_VERSION
from nl2pbip.data_inspector import inspect_data_source
from nl2pbip.dax_catalog import DEFAULT_DAX_LIBRARY_PATH, DAXCatalog
from nl2pbip.llm_client import StructuredLLMClient
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.pbir_validator import PBIRValidator
from nl2pbip.prompts import REPORT_GENERATION_PROMPT_VERSION
from nl2pbip.tmdl_engine import MODEL_PATH_KEY


def _require_mcp() -> "Any":
    """Lazy import of :class:`FastMCP` with a clear install hint.

    The MCP SDK ships as the optional ``[mcp]`` extra so the rest
    of the package stays slim for users who don't need the server.
    Importing :mod:`nl2pbip.mcp_server` must NOT require the SDK
    installed — only :func:`build_server` (and the derived
    :class:`Nl2PbipMcpServer`) should raise when the extra is
    missing. CI's ``.[dev]`` install therefore keeps working
    while the ``[mcp]``-gated tests skip cleanly via
    :func:`pytest.importorskip`.
    """

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "The MCP server requires the optional 'mcp' extra: "
            'install with `pip install "nl2pbip[mcp]"`.'
        ) from exc
    return FastMCP


# ---------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------


def build_server(
    name: str = "nl2pbip",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    """Construct a configured :class:`FastMCP` exposing the four tools.

    A factory (not a module-level singleton) makes the server
    trivially mockable for tests: ``build_server()`` returns a fresh
    instance with its own tool registry, so a test can introspect
    registered tools via ``await server.list_tools()`` without
    touching the real default server.

    Args:
        name: Server name advertised in the MCP ``initialize``
            response. Defaults to ``"nl2pbip"``.
        host: Bind address for the ``streamable-http`` transport.
            Defaults to ``127.0.0.1`` (loopback only — the v1.6.0
            server trusts the network and assumes the operator
            is exposing it via a trusted tunnel). Set to
            ``0.0.0.0`` only when running behind a reverse proxy
            with appropriate access controls.
        port: TCP port for the ``streamable-http`` transport.
            Defaults to ``8000`` (FastMCP's default). Ignored for
            stdio transport.

    Requires the optional ``[mcp]`` extra; raises :class:`ImportError`
    with an install hint when the SDK is missing. Importing
    :mod:`nl2pbip.mcp_server` itself stays cheap.
    """

    FastMCP = _require_mcp()
    server = FastMCP(
        name=name,
        instructions=_SERVER_INSTRUCTIONS,
        host=host,
        port=port,
    )
    server.tool(
        name="generate_report",
        description=(
            "Generate a Power BI Project (.pbip) folder from a natural "
            "language description. Drives the same Orchestrator as the "
            "CLI's `nl2pbip generate --prompt ...` command. Provider "
            "credentials are picked up from environment variables "
            "(NL2PBIP_LLM_PROVIDER, OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "etc.). Returns the absolute path to the packaged .pbip "
            "folder plus a structured plan summary. Set "
            "`include_artifact=True` to additionally receive the "
            "packaged .pbip as a base64-encoded zip in the response "
            "(useful over streamable-http transport where the client "
            "has no filesystem access to the server)."
        ),
    )(_tool_generate_report)
    server.tool(
        name="validate_pbip",
        description=(
            "Validate an existing Power BI Project (.pbip) folder using "
            "the project's PBIRValidator. Walks every page and visual, "
            "checks schema URIs, projection bindings, and measure "
            "references against the semantic model. Returns a structured "
            "report (errors, warnings, validated counts). No LLM call."
        ),
    )(_tool_validate_pbip)
    server.tool(
        name="inspect_dataset",
        description=(
            "Profile a single data source (CSV / JSON / JSONL / Parquet / "
            "in-memory records) using the project's stdlib-only data "
            "inspector. Returns column profiles (type, null rate, distinct "
            "count, top distinct values), suggested foreign keys, and a "
            "summary useful as planner context for `generate_report`. No "
            "LLM call. Set `redact_distinct_values=True` to strip example "
            "values before sending the profile to a hosted LLM. Each "
            "column profile includes its top 5 most common distinct "
            "values when redaction is off."
        ),
    )(_tool_inspect_dataset)
    server.tool(
        name="version",
        description=(
            "Return the running nl2pbip library version and the report "
            "generation prompt version. Useful for clients that want to "
            "confirm they're talking to a server whose prompt rules match "
            "the documented behaviour."
        ),
    )(_tool_version)
    # ``_require_mcp()`` returns ``Any`` because the SDK is
    # lazy-imported for the optional-extra case. The runtime
    # value IS the FastMCP class — mypy can't follow the
    # dynamic import, so annotate the ignore inline rather
    # than casting (which also requires the FastMCP symbol
    # under TYPE_CHECKING) or leaking the optional-dep
    # abstraction out of the public signature.
    return server  # type: ignore[no-any-return]


_SERVER_INSTRUCTIONS = (
    "nl2pbip MCP server: generate Power BI Project (.pbip) folders "
    "from natural language, validate existing PBIP folders, profile "
    "datasets before authoring reports, and report server versions. "
    "Use generate_report for the full pipeline; validate_pbip and "
    "inspect_dataset are LLM-free helpers suitable for CI; version "
    "is a capability check."
)


# ---------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------


def _tool_generate_report(
    prompt: str,
    workspace: str = "artifacts/workspace",
    output_dir: Optional[str] = None,
    project_name: str = "NL2PBIP",
    max_cost_usd: float = 10.0,
    plan_chunk_size: int = 0,
    dax_library: Optional[str] = None,
    include_artifact: bool = False,
    max_artifact_bytes: int = 50 * 1024 * 1024,
) -> Dict[str, Any]:
    """MCP tool: NL prompt -> packaged .pbip folder.

    Mirrors :func:`nl2pbip.cli._handle_generate` 1:1 so behaviour is
    identical to ``nl2pbip generate --prompt ...`` on the CLI.

    When ``include_artifact=True`` the response additionally
    carries ``artifact_zip_b64`` — a base64-encoded zip archive of
    the packaged ``.pbipdir`` folder — so a remote MCP client
    (e.g. running over streamable-http) can deliver the artifact
    back to its caller without needing filesystem access to the
    server. The zip is only included if it fits under
    ``max_artifact_bytes`` (default 50 MB); larger artifacts fall
    back to returning ``project_path`` only with a warning, so a
    runaway plan can't OOM the JSON-RPC frame.
    """

    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty natural-language description")

    workspace_path = Path(workspace).expanduser()
    workspace_path.mkdir(parents=True, exist_ok=True)
    model_path = workspace_path / "semantic_model" / "model.tmdl"
    report_path = workspace_path / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if output_dir is None:
        output_path = Path("artifacts") / f"pbip-{timestamp}"
    else:
        output_path = Path(output_dir).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    catalog = DAXCatalog.from_file(dax_library or str(DEFAULT_DAX_LIBRARY_PATH))

    llm = StructuredLLMClient()  # env-driven, matches CLI defaults

    effective_max_cost_usd: Optional[float] = max_cost_usd
    if effective_max_cost_usd is not None and effective_max_cost_usd <= 0:
        effective_max_cost_usd = None
    effective_chunk_size: int = max(0, int(plan_chunk_size or 0))

    orchestrator = Orchestrator(
        llm_client=llm,
        dax_catalog=catalog,
        max_cost_usd=effective_max_cost_usd,
        plan_chunk_size=effective_chunk_size,
    )
    register_builtin_tools(orchestrator)

    context: Dict[str, Any] = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
        "package_path": str(output_path),
        "project_name": project_name,
        "overwrite": True,
        "dax_catalog_path": str(catalog.source_path),
    }

    results = orchestrator.run(prompt, context=context)

    plan_summary: List[Dict[str, Any]] = [
        {"tool": result.tool, "output": result.output} for result in results
    ]
    package_outputs = next((res for res in results if res.tool == "package_pbip"), None)
    project_path: Optional[str] = None
    if package_outputs is not None:
        project_path = package_outputs.output.get("project_path")

    artifact_b64: Optional[str] = None
    artifact_filename: Optional[str] = None
    if include_artifact and project_path:
        artifact_b64, artifact_filename, artifact_warning = _encode_pbip_artifact(
            Path(project_path), max_artifact_bytes
        )
    elif include_artifact and not project_path:
        artifact_warning = "no artifact produced: plan did not include package_pbip"
    else:
        artifact_warning = None

    warnings: List[str] = []
    if not project_path:
        warnings.append("plan did not include package_pbip; review LLM output")
    if artifact_warning is not None:
        warnings.append(artifact_warning)

    response: Dict[str, Any] = {
        "status": "ok" if project_path else "plan_incomplete",
        "project_path": project_path,
        "output_dir": str(output_path),
        "workspace": str(workspace_path),
        "plan_steps": len(plan_summary),
        "plan": plan_summary,
        "warnings": warnings,
    }
    if artifact_b64 is not None:
        response["artifact_zip_b64"] = artifact_b64
        response["artifact_filename"] = artifact_filename
    return response


def _encode_pbip_artifact(
    pbip_dir: Path,
    max_bytes: int,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Zip a ``.pbipdir`` folder into base64 if it fits under ``max_bytes``.

    Returns ``(b64_payload, filename, warning)``:

    - ``b64_payload``: base64-encoded zip bytes, or ``None`` if the
      artifact was too large (or doesn't exist). When ``None`` the
      warning carries the reason.
    - ``filename``: suggested filename for the artifact
      (``<pbip_dir_name>.zip``).
    - ``warning``: human-readable string the caller should append
      to the response's ``warnings`` list, or ``None`` if no warning.
    """
    if not pbip_dir.exists():
        return None, None, f"artifact directory missing: {pbip_dir}"

    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(pbip_dir.rglob("*")):
                if path.is_file():
                    # Store paths relative to the .pbipdir root so the
                    # recipient sees <name>.pbip, <name>.SemanticModel/...
                    # etc. when they unzip.
                    zf.write(path, path.relative_to(pbip_dir.parent))
    except OSError as exc:
        return None, None, f"failed to zip artifact: {exc}"

    raw = buf.getvalue()
    if len(raw) > max_bytes:
        size_mb = len(raw) / (1024 * 1024)
        cap_mb = max_bytes / (1024 * 1024)
        return (
            None,
            None,
            f"artifact too large to embed ({size_mb:.1f} MB > {cap_mb:.0f} MB cap); "
            f"client should fetch project_path directly: {pbip_dir}",
        )
    return (
        base64.b64encode(raw).decode("ascii"),
        f"{pbip_dir.name}.zip",
        None,
    )


def _tool_validate_pbip(pbip_path: str) -> Dict[str, Any]:
    """MCP tool: validate an existing .pbip folder with :class:`PBIRValidator`.

    No LLM call — safe to run from CI on every PR.
    """

    pbip_dir = Path(pbip_path).expanduser()
    if not pbip_dir.exists():
        raise FileNotFoundError(f"PBIP directory not found: {pbip_dir}")
    if not pbip_dir.is_dir():
        raise NotADirectoryError(f"PBIP path is not a directory: {pbip_dir}")

    # The PBIRValidator API walks ``report_path`` (a .pbir file or
    # ``.Report`` folder). The .pbip top-level folder convention
    # nests the report under ``<name>.Report/`` and the model under
    # ``<name>.SemanticModel/``. Discover the report folder rather
    # than hardcoding ``report.json`` so this works for any project
    # name (the project's CLI follows the same convention).
    report_candidates = sorted(
        candidate
        for candidate in pbip_dir.iterdir()
        if candidate.is_dir() and candidate.name.endswith(".Report")
    )
    if not report_candidates:
        # Allow pointing at a report folder directly for parity with
        # the validator's own CLI usage.
        if pbip_dir.name.endswith(".Report") or (pbip_dir / "report.json").exists():
            report_root = pbip_dir
        else:
            raise FileNotFoundError(
                f"No .Report folder found under {pbip_dir}; expected "
                "<name>.Report/ following the PBIP layout convention."
            )
    else:
        if len(report_candidates) > 1:
            raise ValueError(
                f"Multiple .Report folders under {pbip_dir}: "
                f"{[c.name for c in report_candidates]}; specify the "
                "exact report folder rather than the .pbip root."
            )
        report_root = report_candidates[0]

    validator = PBIRValidator()
    issues: List[Dict[str, Any]] = []
    pages_validated = 0
    visuals_validated = 0

    # The PBIR layout convention embeds visuals INSIDE each
    # ``page.json`` under ``visualContainers`` (no separate
    # ``visuals/*.json`` files on disk). :meth:`PBIRValidator.validate_page`
    # walks the page schema and cascades into every embedded
    # visual, so we count validated visuals there too.
    page_files = sorted(report_root.rglob("pages/*/page.json"))
    for page_file in page_files:
        try:
            page_payload = json.loads(page_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(
                {
                    "severity": "error",
                    "path": str(page_file.relative_to(pbip_dir)),
                    "message": f"invalid JSON: {exc.msg} (line {exc.lineno})",
                }
            )
            continue
        try:
            validator.validate_page(page_payload)
            pages_validated += 1
            visuals_validated += len(page_payload.get("visualContainers", []) or [])
        except Exception as exc:  # PBIRValidationError + JSON schema issues
            issues.append(
                {
                    "severity": "error",
                    "path": str(page_file.relative_to(pbip_dir)),
                    "message": str(exc),
                }
            )

    return {
        "status": "ok" if not issues else "errors_found",
        "pbip_path": str(pbip_dir),
        "report_root": str(report_root),
        "pages_validated": pages_validated,
        "visuals_validated": visuals_validated,
        "errors": sum(1 for issue in issues if issue["severity"] == "error"),
        "warnings": sum(1 for issue in issues if issue["severity"] == "warning"),
        "issues": issues,
    }


def _tool_inspect_dataset(
    source: str,
    max_rows: int = 1000,
    redact_distinct_values: bool = False,
) -> Dict[str, Any]:
    """MCP tool: profile a data source for the planner.

    ``source`` is a filesystem path to CSV / JSON / JSONL / Parquet.
    For in-memory records, prefer the Python API — the MCP boundary
    is JSON, so passing records through would require a
    well-defined serialisation we don't want to bake in yet.

    Per-column ``top_n`` is fixed at 5 inside :func:`inspect_data_source`;
    we don't expose it as an MCP argument so the surface stays small.
    """

    if max_rows <= 0:
        raise ValueError("max_rows must be positive")

    profile = inspect_data_source(
        source,
        max_rows=max_rows,
        redact_distinct_values=redact_distinct_values,
    )
    return profile.to_json(redact_distinct_values=redact_distinct_values)


def _tool_version() -> Dict[str, Any]:
    """MCP tool: report server + library + prompt versions."""

    return {
        "server_name": "nl2pbip-mcp",
        "library_version": _NL2PBIP_VERSION,
        "prompt_version": REPORT_GENERATION_PROMPT_VERSION,
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "transport": "stdio",
    }


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


class Nl2PbipMcpServer:
    """Thin convenience wrapper around :func:`build_server`.

    Mirrors the construction pattern of the rest of the project
    (the CLI builds an :class:`Orchestrator`, the MCP module
    exposes a single class). Tests construct an instance directly
    to inspect the underlying :class:`FastMCP` via
    ``instance.server``; production code calls :func:`build_server`
    directly to get the raw FastMCP for ``run()``.
    """

    def __init__(
        self,
        name: str = "nl2pbip",
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.server: FastMCP = build_server(name=name, host=host, port=port)

    def run(
        self,
        transport: Literal["stdio", "streamable-http"] = "stdio",
    ) -> None:
        """Proxy to :meth:`FastMCP.run` for symmetry with the CLI entry point.

        ``stdio`` (default): blocks until the client closes stdin.
        ``streamable-http``: serves over HTTP at ``self.host:self.port``.
        """

        self.server.run(transport=transport)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the MCP server.

    Invoked by ``python -m nl2pbip.mcp_server`` (and the console
    script ``nl2pbip-mcp`` registered in pyproject.toml). Blocks
    until the client closes the connection or the process
    receives SIGINT/SIGTERM.

    CLI flags (parsed from ``argv`` or ``sys.argv[1:]``):

    ``--transport {stdio,streamable-http}``
        Wire protocol. Default ``stdio``.
    ``--host HOST``
        Bind address for ``streamable-http``. Default ``127.0.0.1``.
    ``--port PORT``
        TCP port for ``streamable-http``. Default ``8000``.
    """

    import argparse

    parser = argparse.ArgumentParser(prog="nl2pbip-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Wire protocol (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for streamable-http (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port for streamable-http (default: 8000).",
    )
    args = parser.parse_args(argv)

    server = build_server(host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
