"""Export engine that compiles PBIP folders into PBIX/PBIT artifacts.

This module provides two paths:

1. **pbi-tools path** — ``export_with_pbi_tools`` shells out to the
   official ``pbi-tools`` CLI for guaranteed-correct ``.pbix`` /
   ``.pbit`` output. Use this in production.

2. **Pure-Python fallback** — ``export_as_pbit_zip`` builds an
   OPC-compliant ``.pbit`` archive directly from a PBIP folder.
   The output is a valid Power BI Desktop template: the
   ``[Content_Types].xml`` and manifest parts (``Version``,
   ``Metadata``, ``Settings``, ``SecurityBindings``,
   ``DiagramLayout``) follow the canonical Power BI layout.
   ``DataModelSchema`` and ``DataMashup`` are emitted as minimal
   stubs because compiling TMDL to TMSL is non-trivial without
   pbi-tools — Power BI Desktop opens the file as a template and
   prompts the user for a data source on first open.

The fallback implementation lives in :mod:`nl2pbip.exporter.opc`
and :mod:`nl2pbip.exporter.pbit_builder`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nl2pbip.exporter.pbit_builder import PbitArchiveBuilder

_LOGGER = logging.getLogger(__name__)


class PBIPExporter:
    """Handles PBIP export via pbi-tools or zipped fallbacks."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or _LOGGER

    # ------------------------------------------------------------------
    # pbi-tools integration
    # ------------------------------------------------------------------
    def export_with_pbi_tools(
        self, pbip_path: str, format: str, output_path: str
    ) -> bool:
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
                self._logger.error(
                    "pbi-tools compile failed with exit code %s", result.returncode
                )
            return False
        if result.stdout:
            self._logger.debug(result.stdout.strip())
        return True

    # ------------------------------------------------------------------
    # Fallback ZIP exporter
    # ------------------------------------------------------------------
    def export_as_pbit_zip(self, pbip_path: str, output_path: str) -> str:
        """Create a Power BI Desktop template (.pbit) from a PBIP folder.

        This is a best-effort fallback for environments where
        ``pbi-tools`` is not installed. The resulting archive follows
        the canonical Power BI Desktop OPC layout:

        * ``[Content_Types].xml`` at the archive root (with UTF-8 BOM).
        * Forward-slash paths (``Report/Layout``, not ``Report\\Layout``).
        * Manifest parts: ``Version``, ``Metadata``, ``Settings``,
          ``SecurityBindings``, ``DiagramLayout``.
        * Model parts: ``DataModelSchema`` (TMSL JSON), ``DataMashup``
          (8-byte header + embedded OPC ZIP).
        * Report parts: ``Report/Layout`` (JSON), optional
          ``Report/StaticResources/...`` and ``Report/CustomVisuals/...``.

        ``DataModelSchema`` is emitted as a minimal TMSL shell —
        Power BI Desktop opens the file as a template and prompts
        the user for a data source on first open. For a fully
        populated model, use ``export_with_pbi_tools``.

        Returns the absolute path to the produced ``.pbit`` file.
        """
        pbip_dir = Path(pbip_path).expanduser()
        if not pbip_dir.exists():
            raise FileNotFoundError(f"PBIP directory not found: {pbip_dir}")

        # Extract the project name from the ``*.pbip`` opener file.
        # Power BI Desktop requires the template name to match the
        # original PBIP project name.
        project_name = _extract_project_name(pbip_dir)

        output_file = Path(output_path).with_suffix(".pbit")
        output_file.parent.mkdir(parents=True, exist_ok=True)

        builder = PbitArchiveBuilder(
            output_path=output_file,
            template_name=project_name,
        )

        # Carry the PBIP folder contents into the builder.
        builder.add_pbip_folder(pbip_dir)

        # Override metadata with creation timestamp so the
        # "New report from template" dialog shows the right date.
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        builder.add_metadata(
            title=project_name,
            description=f"Generated by nl2pbip at {timestamp}.",
            created=timestamp,
            generator="nl2pbip",
        )

        builder.finalize()
        self._logger.info("Created fallback PBIT archive at %s", output_file)
        return str(output_file)

    # ------------------------------------------------------------------
    # Direct builder accessor (used by tests + advanced callers)
    # ------------------------------------------------------------------
    def build_pbit_archive(
        self,
        pbip_path: str,
        output_path: str,
        *,
        template_name: Optional[str] = None,
    ) -> str:
        """Build an OPC-compliant ``.pbit`` from a PBIP folder.

        Returns the path to the produced file. Unlike
        :meth:`export_as_pbit_zip`, this method exposes the
        underlying :class:`PbitArchiveBuilder` via a callback —
        callers can add custom parts (e.g. custom visuals from a
        different source) before finalisation.

        The default ``template_name`` is the project name extracted
        from the ``*.pbip`` opener file.
        """
        pbip_dir = Path(pbip_path).expanduser()
        if not pbip_dir.exists():
            raise FileNotFoundError(f"PBIP directory not found: {pbip_dir}")
        if template_name is None:
            template_name = _extract_project_name(pbip_dir)

        builder = PbitArchiveBuilder(
            output_path=Path(output_path).with_suffix(".pbit"),
            template_name=template_name,
        )
        builder.add_pbip_folder(pbip_dir)
        return str(builder.finalize())


def _extract_project_name(pbip_dir: Path) -> str:
    """Find the project name from the PBIP folder's opener file.

    Power BI Desktop writes a single ``*.pbip`` opener file at the
    root of the PBIP folder containing a JSON object with a
    ``name`` field. We read that field and use it as the template
    name.
    """
    for candidate in pbip_dir.iterdir():
        if candidate.suffix.lower() == ".pbip" and candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            name = payload.get("name")
            if isinstance(name, str) and name:
                return name
    return pbip_dir.name


__all__ = [
    "PBIPExporter",
]
