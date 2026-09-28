#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Script that the README demo terminal records. Drives nl2pbip's
# no-LLM example, then inspects the produced .pbipdir structure.
# Intended for ``asciinema rec --command <this-file>`` so the
# recording captures every character as if the viewer were typing.
# ---------------------------------------------------------------------
set -e

# Asciinema requires a sane TERM so the captured escape sequences
# stay meaningful when agg re-renders the .cast back to pixels.
export TERM="${TERM:-xterm-256color}"

# Project root.
cd /Users/hermes/work/nl2pbip

# Brief header so the recording has clear context.
clear
echo "# nl2pbip — natural language → Power BI Project (.pbip)"
echo "# Full pipeline demo: prompt → TMDL + PBIR → git-friendly .pbip"
echo
sleep 2

# Step 1: install hint + version check. We don't actually run pip
# here (the recording would be too long and the user's environment
# already has nl2pbip installed).
echo "# Already installed: nl2pbip $(.venv/bin/python -c 'import nl2pbip; print(nl2pbip.__version__)')"
sleep 1.5

# Step 2: headline no-LLM demo.
echo "$ .venv/bin/python -m nl2pbip.example_run"
.venv/bin/python -m nl2pbip.example_run
sleep 2

# Step 3: show the produced .pbipdir structure.
echo
echo "$ tree -L 4 artifacts/SalesInsights.pbipdir"
echo
which tree >/dev/null 2>&1 && tree -L 4 artifacts/SalesInsights.pbipdir || \
    (cd artifacts && find SalesInsights.pbipdir -maxdepth 4 -print | sort | sed 's|[^/]*/|  |g; s|^  ||')

echo
echo "# Every file is plain JSON or plain text — git-diff friendly."
echo "# Open in Power BI Desktop: File → Open → *.pbip"
echo
sleep 2

echo "# LLM-driven, MCP, telemetry, and 1000+ tests:"
echo "# github.com/rollroyces/nl2pbip"
sleep 3