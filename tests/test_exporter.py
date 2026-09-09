from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nl2pbip.exporter import PBIPExporter


@patch("nl2pbip.exporter.exporter.shutil.which", return_value="/usr/local/bin/pbi-tools")
@patch("nl2pbip.exporter.exporter.subprocess.run")
def test_export_with_pbi_tools_invokes_cli(mock_run, mock_which, tmp_path: Path) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    exporter = PBIPExporter()

    result = exporter.export_with_pbi_tools(str(tmp_path / "proj"), "pbix", str(tmp_path / "out.pbix"))

    assert result is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[:2] == ["pbi-tools", "compile"]


@patch("nl2pbip.exporter.exporter.shutil.which", return_value=None)
@patch("nl2pbip.exporter.exporter.subprocess.run")
def test_export_with_pbi_tools_no_binary(mock_run, mock_which) -> None:
    exporter = PBIPExporter()

    result = exporter.export_with_pbi_tools("/path/to/proj", "pbit", "/tmp/out.pbit")

    assert result is False
    mock_run.assert_not_called()


def test_export_as_pbit_zip_packages_directories(tmp_path: Path) -> None:
    pbip_dir = tmp_path / "Proj"
    dataset_dir = pbip_dir / "MyProject.SemanticModel"
    report_dir = pbip_dir / "MyProject.Report"
    (dataset_dir / "definition").mkdir(parents=True)
    (report_dir / "definition").mkdir(parents=True)
    (dataset_dir / "definition" / "model.tmdl").write_text("model", encoding="utf-8")
    (report_dir / "definition" / "report.json").write_text(json.dumps({"pages": []}), encoding="utf-8")

    exporter = PBIPExporter()
    output_path = pbip_dir / "template.pbit"
    artifact = exporter.export_as_pbit_zip(str(pbip_dir), str(output_path))

    assert Path(artifact).exists()
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        assert any(name.startswith("Dataset/") for name in names)
        assert any(name.startswith("Report/") for name in names)
        assert "Metadata.json" in names
*** End File content***