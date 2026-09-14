#!/usr/bin/env python3
"""Convert Bandit's JSON output to SARIF 2.1.0.

Bandit 1.9 doesn't emit SARIF natively (the --format choices
are csv/custom/html/json/screen/txt/xml/yaml). This small
wrapper keeps the Bandit -> Security-tab pipeline working
without adding a runtime dep on sarif-tools or similar.

Invoked by .github/workflows/bandit.yml. Reads ``bandit-results.json``
from the current working directory and writes ``bandit-results.sarif``.
Exit code 0 even on zero findings (an empty SARIF document is
valid; the upload-sarif step just publishes an empty report).
"""

import json
import sys
from pathlib import Path

SEVERITY_TO_SARIF_LEVEL = {
    "LOW": "note",
    "MEDIUM": "warning",
    "HIGH": "error",
}


def main() -> int:
    src = Path("bandit-results.json")
    if not src.exists():
        # Bandit didn't run (or no results). Write a stub so the
        # upload-sarif step doesn't fail.
        Path("bandit-results.sarif").write_text(
            json.dumps(
                {
                    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
                    "version": "2.1.0",
                    "runs": [],
                }
            )
        )
        return 0

    with src.open() as f:
        data = json.load(f)

    results = data.get("results", [])
    rule_ids = sorted({r["test_id"] for r in results})

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Bandit",
                        "version": data.get("metrics", {})
                        .get("_info", {})
                        .get("version", "1.9.4"),
                        "informationUri": "https://bandit.readthedocs.io/",
                        "rules": [
                            {
                                "id": rid,
                                "name": rid,
                                "shortDescription": {"text": rid},
                                "defaultConfiguration": {"level": "warning"},
                            }
                            for rid in rule_ids
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": r["test_id"],
                        "level": SEVERITY_TO_SARIF_LEVEL.get(
                            r["issue_severity"], "note"
                        ),
                        "message": {"text": r["issue_text"]},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": r["filename"]},
                                    "region": {"startLine": r["line_number"]},
                                }
                            }
                        ],
                    }
                    for r in results
                ],
            }
        ],
    }
    Path("bandit-results.sarif").write_text(json.dumps(sarif, indent=2))
    print(f"wrote SARIF with {len(results)} findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
