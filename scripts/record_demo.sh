#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Regenerate every README terminal recording from a clean workspace.
#
#   assets/demo            no-LLM `python -m nl2pbip.example_run`
#   assets/llm_demo       LLM-driven Python API with a mock client
#   assets/mcp_demo       MCP stdio JSON-RPC exchange (initialize,
#                         tools/list, version, validate_pbip)
#
# All three are recorded via [asciinema], then rendered to GIF + MP4
# via [agg] + ffmpeg. The README references them as `assets/demo.{gif,mp4}`,
# `assets/llm_demo.{gif,mp4}`, and `assets/mcp_demo.{gif,mp4}`.
#
# Required binaries (no brew needed — everything is downloaded to
# ~/.hermes):
#   * ~/.hermes/bin/asciinema           asciinema-aarch64-apple-darwin
#   * ~/.hermes/bin/agg                 agg-aarch64-apple-darwin
#   * ~/.hermes/tools/ffmpeg-*/ffmpeg   vendored binary
#
# Re-run this script after a pipeline change to refresh the assets
# committed to git.
# ---------------------------------------------------------------------
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

ASCIINEMA="${ASCIINEMA:-$HOME/.hermes/bin/asciinema}"
AGG="${AGG:-$HOME/.hermes/bin/agg}"
FFMPEG="${FFMPEG:-$HOME/.hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg}"

for bin in "$ASCIINEMA" "$AGG" "$FFMPEG"; do
    if [[ ! -x "$bin" ]]; then
        echo "error: required binary not found or not executable: $bin" >&2
        exit 1
    fi
done

render_one() {
    local name="$1"
    local cmd="$2"
    local prep="${3:-}"
    local idle="${4:-2.5}"

    echo "=== Recording $name ==="
    if [[ -n "$prep" ]]; then
        echo "    prep: $prep"
        eval "$prep"
    fi

    # cols 130 / rows 36 matches the original demo. The idle-time-limit
    # collapses long sleeps; pass a larger limit for scripts with
    # intentional pauses between output bursts (e.g. llm_demo).
    TERM=xterm-256color "$ASCIINEMA" rec --quiet \
        --idle-time-limit "$idle" \
        --cols 130 --rows 36 \
        --command "$cmd" \
        "assets/${name}.cast"

    "$AGG" "assets/${name}.cast" "assets/${name}.gif" \
        --theme dracula --font-size 14 --cols 130 --rows 36

    "$FFMPEG" -y -loglevel error \
        -i "assets/${name}.gif" \
        -movflags +faststart \
        -pix_fmt yuv420p \
        -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
        "assets/${name}.mp4"

    ls -la "assets/${name}.cast" "assets/${name}.gif" "assets/${name}.mp4"
}

PREP_DEMO="rm -rf artifacts/report_workspace.json artifacts/semantic_workspace artifacts/SalesInsights.pbipdir"
PREP_LLM="rm -rf artifacts/llm_workspace artifacts/SalesInsights.pbipdir artifacts/CustomerInsights.pbipdir"
# MCP demo uses the .pbip dir produced by demo_script.sh — record
# that first so the MCP demo has something to validate.

render_one "demo"      "bash assets/demo_script.sh"            "$PREP_DEMO" 2.5
render_one "llm_demo"  ".venv/bin/python assets/llm_demo_script.py" "$PREP_LLM"  5.0
render_one "mcp_demo"  ".venv/bin/python assets/mcp_demo_script.py" ""             5.0

echo
echo "OK — refreshed:"
ls -la assets/demo.* assets/llm_demo.* assets/mcp_demo.*