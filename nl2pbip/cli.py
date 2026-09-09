"""Command-line interface for generating PBIP projects via natural language."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from nl2pbip.dax_catalog import DEFAULT_DAX_LIBRARY_PATH, DAXCatalog
from nl2pbip.exporter import PBIPExporter
from nl2pbip.llm_client import StructuredLLMClient
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import MODEL_PATH_KEY


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parsed_argv: list[str]
    if argv is None:
        parsed_argv = list(sys.argv[1:])
    else:
        parsed_argv = list(argv)

    command = "generate"
    if parsed_argv and parsed_argv[0] in {"generate", "export"}:
        command = parsed_argv.pop(0)

    if command == "export":
        parser = _build_export_parser()
        args = parser.parse_args(parsed_argv)
    else:
        parser = _build_generate_parser()
        args = parser.parse_args(parsed_argv)
        command = "generate"
    args.command = command
    return args


def _build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Natural language to PBIP generator.")
    parser.add_argument(
        "--prompt",
        required=True,
        help="Desired report description in natural language.",
    )
    parser.add_argument(
        "--workspace",
        default="artifacts/workspace",
        help="Path to store intermediate semantic/report files.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Target directory for the packaged PBIP project. Defaults to artifacts/<timestamp>.",
    )
    parser.add_argument(
        "--project-name",
        default="NL2PBIP",
        help="Human-readable project name for manifests.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=[
            "openai",
            "azure",
            "anthropic",
            "deepseek",
            "qwen",
            "zhipu",
            "moonshot",
            "custom",
        ],
        help="LLM provider identifier (defaults to NL2PBIP_LLM_PROVIDER or OpenAI).",
    )
    parser.add_argument(
        "--model", default=None, help="Model name to request from the provider."
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the provider base URL / endpoint (required for Azure/custom/local deployments if no env var).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Override the API key for the selected provider (otherwise inferred from provider-specific env vars).",
    )
    parser.add_argument(
        "--api-version",
        default=None,
        help="Azure OpenAI API version override (defaults to 2024-06-01). Ignored for other providers.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing PBIP output directory if present.",
    )
    parser.add_argument(
        "--dax-library",
        default=str(DEFAULT_DAX_LIBRARY_PATH),
        help="Path to the organizational DAX pattern library JSON file.",
    )
    parser.add_argument(
        "--export",
        choices=["pbix", "pbit"],
        dest="export_format",
        help="Optional artifact format to compile immediately after generation.",
    )
    parser.add_argument(
        "--export-output",
        dest="export_output",
        default=None,
        help="Optional path for the exported PBIX/PBIT file. Defaults to <pbip_dir>.<format>.",
    )
    return parser


def _build_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export existing PBIP folders to PBIX/PBIT."
    )
    parser.add_argument(
        "--input", required=True, help="Path to the PBIP directory to compile."
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=["pbix", "pbit"],
        help="Desired export format.",
    )
    parser.add_argument("--output", required=True, help="Output PBIX/PBIT path.")
    return parser


def _handle_generate(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    model_path = workspace / "semantic_model" / "model.tmdl"
    report_path = workspace / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = (
        Path(args.output).expanduser()
        if args.output
        else Path("artifacts") / f"pbip-{timestamp}"
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    context: Dict[str, Any] = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
        "package_path": str(output_dir),
        "project_name": args.project_name,
        "overwrite": args.overwrite,
    }

    catalog = DAXCatalog.from_file(args.dax_library)
    context["dax_catalog_path"] = str(catalog.source_path)

    llm = StructuredLLMClient(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        api_version=args.api_version,
    )
    orchestrator = Orchestrator(llm_client=llm, dax_catalog=catalog)
    register_builtin_tools(orchestrator)

    results = orchestrator.run(args.prompt, context=context)

    print("Executed plan:")
    for result in results:
        print(f"- {result.tool}: {json.dumps(result.output)}")

    package_outputs = next((res for res in results if res.tool == "package_pbip"), None)
    if package_outputs:
        project_path = package_outputs.output.get("project_path")
        print("PBIP ready at:", project_path)
        if args.export_format:
            if not project_path:
                raise RuntimeError(
                    "package_pbip result missing project_path; cannot export."
                )
            export_target = args.export_output or f"{project_path}.{args.export_format}"
            _run_export(project_path, args.export_format, export_target)
    else:
        print("Warning: plan did not include package_pbip. Please review LLM output.")


def _handle_export(args: argparse.Namespace) -> None:
    exporter = PBIPExporter()
    if exporter.export_with_pbi_tools(args.input, args.format, args.output):
        print(f"Exported {args.format} to {args.output}")
        return
    if args.format != "pbit":
        raise RuntimeError(
            "pbi-tools is required to export PBIX files. Install pbi-tools or choose --format pbit for fallback zip generation."
        )
    artifact_path = exporter.export_as_pbit_zip(args.input, args.output)
    print(f"Fallback PBIT archive created at {artifact_path}")


def _run_export(pbip_path: str, export_format: str, output_path: str) -> None:
    exporter = PBIPExporter()
    if exporter.export_with_pbi_tools(pbip_path, export_format, output_path):
        print(f"Exported {export_format} to {output_path}")
        return
    if export_format != "pbit":
        raise RuntimeError(
            "pbi-tools is required for PBIX exports when running 'generate --export'. Install pbi-tools CLI to continue."
        )
    artifact_path = exporter.export_as_pbit_zip(pbip_path, output_path)
    print(f"Fallback PBIT archive created at {artifact_path}")


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    if args.command == "export":
        _handle_export(args)
    else:
        _handle_generate(args)


if __name__ == "__main__":
    main()
