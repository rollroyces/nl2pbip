#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Regenerate README recording assets.
#
# Terminal recording (assets/demo.{cast,gif,mp4}):
#   `python -m nl2pbip.example_run` against a clean workspace.
#   Recorded via [asciinema], rendered to GIF + MP4 via [agg] + ffmpeg.
#
# Architecture walkthrough (assets/architecture.{png,mp4}):
#   Browser-rendered camera pan over assets/architecture.html.
#   The HTML file is the source of truth — open it in any browser,
#   hover any component to read its tooltip. The MP4 is a static
#   view of the same diagram with the camera moving through it.
#
# Required binaries (downloadable, no brew required):
#   * ~/.hermes/bin/asciinema           asciinema-aarch64-apple-darwin
#   * ~/.hermes/bin/agg                 agg-aarch64-apple-darwin
#   * ~/.hermes/tools/ffmpeg-*/ffmpeg   vendored binary
#
# Re-run this after a pipeline change to refresh the assets
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

echo "=== Recording terminal demo ==="
PREP_DEMO="rm -rf artifacts/report_workspace.json artifacts/semantic_workspace artifacts/SalesInsights.pbipdir"
eval "$PREP_DEMO"

TERM=xterm-256color "$ASCIINEMA" rec --quiet \
    --idle-time-limit 2.5 \
    --cols 130 --rows 36 \
    --command "bash assets/demo_script.sh" \
    assets/demo.cast

"$AGG" assets/demo.cast assets/demo.gif \
    --theme dracula --font-size 14 --cols 130 --rows 36

"$FFMPEG" -y -loglevel error \
    -i assets/demo.gif \
    -movflags +faststart \
    -pix_fmt yuv420p \
    -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
    assets/demo.mp4

ls -la assets/demo.{cast,gif,mp4}

echo
echo "=== Architecture walkthrough ==="
echo "  assets/architecture.html is the source of truth (open in a browser)"
echo "  assets/architecture.png is the static PNG export (2640×735)"
echo "  assets/architecture.mp4 is the camera-pan walkthrough"
echo
echo "  To regenerate: open the HTML in a browser, screenshot it to"
echo "  PNG via the browser's devtools or via:"
echo "    $FFMPEG -i ... (record your own browser session, then transcode)"
echo
echo "  See README.md → 'One terminal demo + one architecture walkthrough'"
echo "  for the exact beats / camera positions used in the committed MP4."

echo
echo "OK — refreshed:"
ls -la assets/demo.{cast,gif,mp4} assets/architecture.{html,png,mp4}