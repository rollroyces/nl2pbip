"""Export engine that compiles PBIP folders into PBIX/PBIT artifacts."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

_LOGGER = logging.getLogger(__name__)


class PBIPExporter:
    """Handles PBIP export via pbi-tools or zipped fallbacks."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or _LOGGER

    # ------------------------------------------------------------------
    # pbi-tools integration
    # ------------------------------------------------------------------
    def export_with_pbi_tools(self, pbip_path: str, format: str, output_path: str) -> bool:
        """Compile a PBIP folder using pbi-tools.

        Returns True when the external command succeeds, otherwise False.
        """

        fmt = format.lower()
        if fmt not in {"pbix", "pbit"}:
            raise ValueError("format must be 'pbix' or 'pbit'")
        if shutil.which("pbi-tools") is None:
            self._logger.info("pbi-tools not found on PATH; skipping CLI compilation.")
            return False

        output_file = Path(output_path).expanduser()
        output_file.parent.mkdir(parents=True, exist_ok=True)
        pbip_dir = Path(pbip_path).expanduser()

        cmd = [
            "pbi-tools",
            "compile",
            str(pbip_dir),
            "-outPath",
            str(output_file),
            "-format",
            fmt,
        ]
        self._logger.debug("Running pbi-tools compile: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if stderr:
                self._logger.error("pbi-tools compile failed: %s", stderr)
            else:
                self._logger.error("pbi-tools compile failed with exit code %s", result.returncode)
            return False
        if result.stdout:
            self._logger.debug(result.stdout.strip())
        return True

    # ------------------------------------------------------------------
    # Fallback ZIP exporter
    # ------------------------------------------------------------------
    def export_as_pbit_zip(self, pbip_path: str, output_path: str) -> str:
        """Create a PBIT archive from PBIP components without pbi-tools."""

        pbip_dir = Path(pbip_path).expanduser()
        if not pbip_dir.exists():
            raise FileNotFoundError(f"PBIP directory not found: {pbip_dir}")

        dataset_dir = self._find_component_directory(pbip_dir, (".Dataset", ".SemanticModel"))
        report_dir = self._find_component_directory(pbip_dir, (".Report",))
        if dataset_dir is None:
            raise FileNotFoundError("PBIP folder does not contain a dataset/semantic model component.")
        if report_dir is None:
            raise FileNotFoundError("PBIP folder does not contain a report component.")

        output_file = Path(output_path).with_suffix(".pbit")
        output_file.parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().isoformat() + "Z"
        metadata = {
            "created": timestamp,
            "source": "nl2pbip",
            "notes": "Generated via fallback exporter.",
        }

        with zipfile.ZipFile(output_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            self._write_directory_to_zip(archive, dataset_dir, "Dataset")
            self._write_directory_to_zip(archive, report_dir, "Report")
            archive.writestr("Metadata.json", json.dumps(metadata, indent=2))

        self._logger.info("Created fallback PBIT archive at %s", output_file)
        return str(output_file)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _find_component_directory(self, pbip_dir: Path, suffixes: Iterable[str]) -> Optional[Path]:
        for candidate in pbip_dir.iterdir():
            if candidate.is_dir() and any(candidate.name.endswith(suffix) for suffix in suffixes):
                return candidate
        return None

    def _write_directory_to_zip(self, archive: zipfile.ZipFile, source_dir: Path, root_name: str) -> None:
        for file_path in source_dir.rglob("*"):
            if file_path.is_dir():
                continue
            relative_path = file_path.relative_to(source_dir)
            archive_path = Path(root_name) / relative_path
            archive.write(file_path, archive_path.as_posix())
