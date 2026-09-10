"""OPC (Open Packaging Conventions) primitives for Power BI artifacts.

Both ``.pbit`` (template) and ``.pbix`` (report with cached data)
archives are ZIP files that follow the OPC format:

* ``[Content_Types].xml`` at the archive root declares the MIME type
  of every part. Power BI Desktop requires this file to be present;
  archives without it are rejected on open.
* Parts use forward-slash paths (``Report/Layout``, not
  ``Report\\Layout``).
* The XML declaration in ``[Content_Types].xml`` is UTF-8 with a
  leading BOM (``\\xef\\xbb\\xbf``). Power BI Desktop is strict about
  this — archives missing the BOM fail to load.
* OPC package relationships (``_rels/.rels``) are NOT required for
  Power BI archives; Power BI uses content-type defaults and
  ``<Override>`` entries rather than ``.rels`` files.

See :class:`PbitArchiveBuilder` for the high-level writer that
produces a Power BI Desktop-compatible ``.pbit`` template from a
PBIP folder.

The :mod:`nl2pbip.pbir_engine` and :mod:`nl2pbip.tmdl_engine` modules
produce PBIP folder-format artifacts (``TMDL`` text, ``.pbir``
JSON). Converting to ``.pbit`` requires compiling the TMDL into a
Tabular Model JSON (``DataModelSchema``) — that's what ``pbi-tools
compile`` does at authoring time. Without pbi-tools, this module
emits a minimal but OPC-valid shell that Power BI Desktop will
accept; on first open, the user will be prompted to provide a data
source so the model can be recompiled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Content-Type catalogue
# ---------------------------------------------------------------------------

# Default extensions registered by Power BI archives. Power BI Desktop
# accepts both ``application/json`` and an empty content type for
# JSON parts; we use ``application/json`` for explicitness.
_DEFAULT_CONTENT_TYPES: Dict[str, str] = {
    "json": "application/json",
    "xml": "application/xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "css": "text/css",
    "js": "application/javascript",
    "geojson": "application/vnd.microsoft.datamashup.geospatial",
}

# Power BI parts that Power BI Desktop requires to be registered as
# ``Override`` entries with explicit MIME types. The empty content
# type used by older Power BI exports (``ContentType=""``) still
# works for compatibility but the explicit form is preferred.
_KNOWN_OVERRIDES: Dict[str, str] = {
    # Root-level model parts
    "Version": "application/json",
    "Metadata": "application/json",
    "Settings": "application/json",
    "SecurityBindings": "application/json",
    "DiagramLayout": "application/json",
    "DataModelSchema": "application/json",
    "DataMashup": "application/octet-stream",
    # Report parts
    "Report/Layout": "application/json",
    "Report/LinguisticSchema": "application/xml",
}


@dataclass
class _ContentTypeRegistry:
    """Build a ``[Content_Types].xml`` payload for an archive.

    Tracks ``Default`` entries (extension → MIME) and ``Override``
    entries (part path → MIME). Both are required: ``Default``
    entries cover unknown parts by extension, ``Override`` entries
    pin a specific MIME for known parts that Power BI Desktop
    looks up by path.
    """

    defaults: Dict[str, str] = field(default_factory=dict)
    overrides: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_power_bi(cls) -> "_ContentTypeRegistry":
        """Return the canonical Power BI Desktop content-type registry.

        Includes all the ``Default`` extensions Power BI uses plus
        the ``Override`` entries for known parts. Callers can add
        extra ``Override`` entries (e.g. for custom visuals) before
        emitting the XML.
        """
        registry = cls(defaults=dict(_DEFAULT_CONTENT_TYPES))
        registry.overrides.update(_KNOWN_OVERRIDES)
        return registry

    def add_override(self, part_path: str, content_type: str) -> None:
        """Register an explicit MIME type for a specific part path."""
        self.overrides[part_path] = content_type

    def add_default(self, extension: str, content_type: str) -> None:
        """Register a default MIME type for an extension."""
        self.defaults[extension] = content_type

    def to_xml(self) -> bytes:
        """Render the registry as a ``[Content_Types].xml`` byte string.

        Power BI Desktop requires:
        * the UTF-8 BOM (``\\xef\\xbb\\xbf``) prefix on the XML
          declaration, otherwise the archive fails to load with a
          vague "file is corrupt" error;
        * the OPC namespace (``http://schemas.openxmlformats.org/package/2006/content-types``).
        """
        lines: List[str] = []
        lines.append('<?xml version="1.0" encoding="utf-8"?>')
        lines.append(
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        )
        for ext, ctype in sorted(self.defaults.items()):
            lines.append(f'  <Default Extension="{ext}" ContentType="{ctype}" />')
        for path, ctype in sorted(self.overrides.items()):
            lines.append(f'  <Override PartName="/{path}" ContentType="{ctype}" />')
        lines.append("</Types>")
        xml = "\n".join(lines) + "\n"
        # UTF-8 BOM is mandatory — Power BI Desktop rejects archives
        # without it.
        return b"\xef\xbb\xbf" + xml.encode("utf-8")


# ---------------------------------------------------------------------------
# Power BI Desktop template manifest values
# ---------------------------------------------------------------------------

# Compatibility level 1567 corresponds to Power BI Desktop 2022+
# (Analysis Services 15.x). Power BI Desktop accepts this value for
# all current builds.
DEFAULT_COMPATIBILITY_LEVEL = 1567

# Default model culture. ``en-US`` matches what Power BI Desktop
# writes for templates created in the English locale.
DEFAULT_CULTURE = "en-US"


def make_minimal_data_model_schema(
    name: str = "nl2pbip Generated Model",
    compatibility_level: int = DEFAULT_COMPATIBILITY_LEVEL,
    culture: str = DEFAULT_CULTURE,
) -> dict:
    """Return a minimal Tabular Model JSON for a template.

    The real ``DataModelSchema`` payload is a full Tabular Model
    Scripting Language (TMSL) document — see the AS documentation.
    For the template shell we only need to declare the bare minimum
    so Power BI Desktop recognises the archive as a valid template
    and prompts the user for a data source on first open.
    """
    return {
        "name": name,
        "compatibilityLevel": compatibility_level,
        "model": {
            "culture": culture,
            "sourceQueryCulture": culture,
            "defaultMode": "import",
        },
    }


def make_metadata_json(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    created: Optional[str] = None,
    generator: str = "nl2pbip",
) -> dict:
    """Return a template ``Metadata.json`` payload.

    The ``Metadata`` part of a ``.pbit`` archive holds user-supplied
    template metadata: title, description, and the timestamp the
    template was created. Power BI Desktop surfaces this in the
    "New report from template" dialog.
    """
    return {
        "title": title or "nl2pbip Generated Template",
        "description": description or "Template generated by nl2pbip.",
        "created": created or "",
        "generator": generator,
    }


def make_settings_json() -> dict:
    """Return the minimal ``Settings`` payload Power BI expects."""
    return {
        "defaultDrillThroughOther": "TopCount",
        "persistentQueriesEnabled": False,
    }


def make_security_bindings_json() -> dict:
    """Return an empty ``SecurityBindings`` payload.

    Power BI writes an empty list here when no role bindings are
    defined; we do the same.
    """
    return {"roleBindings": []}


def make_diagram_layout_json() -> dict:
    """Return a minimal ``DiagramLayout`` payload.

    Power BI Desktop's model diagram view is optional; the empty
    layout is what Power BI itself writes for new templates.
    """
    return {"diagrams": []}


def make_data_mashup_stub() -> bytes:
    """Return an empty ``DataMashup`` body.

    ``DataMashup`` is an 8-byte magic header (``\\x01\\x00\\x00\\x00\\x01\\x00\\x00\\x00``)
    followed by an embedded OPC ZIP archive of Power Query M
    expressions. For the template shell we emit just the header so
    Power BI Desktop recognises the part and lets the user wire up
    data sources on first open.

    Reference: https://bengribaudo.com/blog/2020/04/22/5198/data-mashup-binary-stream
    """
    # The 8-byte header identifies the file as a DataMashup stream.
    # Power BI Desktop checks this header before attempting to parse
    # the embedded ZIP — without it, the archive is rejected as
    # corrupt.
    return b"\x01\x00\x00\x00\x01\x00\x00\x00"


def make_version_part() -> bytes:
    """Return the ``Version`` part body.

    Real Power BI Desktop writes a single integer (the format
    version). ``1`` is the value Power BI used for templates
    created in 2022+ builds.
    """
    return b"1"


def make_data_model_schema_template(
    name: str = "nl2pbip Generated Model",
    compatibility_level: int = DEFAULT_COMPATIBILITY_LEVEL,
    culture: str = DEFAULT_CULTURE,
) -> dict:
    """Return the ``DataModelSchemaTemplate.json`` payload.

    This is the part that the legacy ``export_as_pbit_zip`` used to
    write. Real Power BI Desktop archives don't include this part
    (they put the model schema in ``DataModelSchema`` instead) but
    the file is still recognised by some readers; we keep emitting
    it for compatibility.
    """
    return make_minimal_data_model_schema(
        name=name,
        compatibility_level=compatibility_level,
        culture=culture,
    )


__all__ = [
    "DEFAULT_COMPATIBILITY_LEVEL",
    "DEFAULT_CULTURE",
    "make_data_mashup_stub",
    "make_data_model_schema_template",
    "make_diagram_layout_json",
    "make_metadata_json",
    "make_minimal_data_model_schema",
    "make_security_bindings_json",
    "make_settings_json",
    "make_version_part",
]
