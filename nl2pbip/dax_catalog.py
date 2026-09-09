"""Load and manage organizational DAX templates and calculation groups."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DAX_LIBRARY_PATH = Path(__file__).with_name("dax_library.json")
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


@dataclass(frozen=True)
class DAXPattern:
    key: str
    description: str
    template: str
    format_string: Optional[str]
    parameters: List[str]


@dataclass(frozen=True)
class CalculationGroupConfig:
    key: str
    name: str
    table_name: str
    precedence: int
    description: Optional[str]
    items: List[Dict[str, Any]]


class DAXCatalog:
    """Central registry for organization-approved DAX assets."""

    def __init__(
        self,
        patterns: Dict[str, DAXPattern],
        calculation_groups: Dict[str, CalculationGroupConfig],
        source_path: Path,
    ):
        self._patterns = patterns
        self._calculation_groups = calculation_groups
        self.source_path = source_path

    @classmethod
    def from_file(cls, path: Optional[str] = None) -> "DAXCatalog":
        file_path = Path(path).expanduser() if path else DEFAULT_DAX_LIBRARY_PATH
        if not file_path.exists():
            raise FileNotFoundError(f"DAX library file not found at {file_path}.")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        patterns = {
            key: DAXPattern(
                key=key,
                description=value.get("description", ""),
                template=value["template"],
                format_string=value.get("format_string"),
                parameters=value.get("parameters")
                or _infer_parameters(value["template"]),
            )
            for key, value in (data.get("patterns") or {}).items()
        }
        calc_groups = {
            key: CalculationGroupConfig(
                key=key,
                name=value.get("name", key),
                table_name=value.get("table_name", value.get("name", key)),
                precedence=int(value.get("precedence", 0)),
                description=value.get("description"),
                items=value.get("items", []),
            )
            for key, value in (data.get("calculation_groups") or {}).items()
        }
        return cls(
            patterns=patterns, calculation_groups=calc_groups, source_path=file_path
        )

    # ------------------------------------------------------------------
    # Pattern helpers
    # ------------------------------------------------------------------
    def available_patterns(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": pattern.key,
                "description": pattern.description,
                "parameters": pattern.parameters,
                "format_string": pattern.format_string,
            }
            for pattern in self._patterns.values()
        ]

    def available_calculation_groups(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": group.key,
                "name": group.name,
                "table_name": group.table_name,
                "precedence": group.precedence,
                "items": [item.get("name") for item in group.items],
            }
            for group in self._calculation_groups.values()
        ]

    def render_pattern(self, pattern_key: str, **params: str) -> Dict[str, Any]:
        pattern = self._patterns.get(pattern_key)
        if not pattern:
            raise ValueError(f"Pattern '{pattern_key}' not found in DAX catalog.")
        missing = [param for param in pattern.parameters if param not in params]
        if missing:
            raise ValueError(
                f"Pattern '{pattern_key}' requires parameters: {', '.join(missing)}"
            )
        expression = _substitute_template(pattern.template, params)
        return {
            "expression": expression,
            "format_string": pattern.format_string,
        }

    def get_calculation_group(self, group_key: str) -> CalculationGroupConfig:
        group = self._calculation_groups.get(group_key)
        if not group:
            raise ValueError(
                f"Calculation group '{group_key}' not found in DAX catalog."
            )
        return group

    def prompt_payload(self) -> Dict[str, Any]:
        return {
            "patterns": self.available_patterns(),
            "calculation_groups": self.available_calculation_groups(),
        }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _infer_parameters(template: str) -> List[str]:
    return sorted(set(PLACEHOLDER_PATTERN.findall(template)))


def _substitute_template(template: str, params: Dict[str, str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise ValueError(f"Missing parameter '{key}' for DAX template.")
        return params[key]

    return PLACEHOLDER_PATTERN.sub(replacer, template)
