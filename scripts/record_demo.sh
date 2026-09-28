#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Regenerate the README's 30-second terminal demo recording.
#
# Records `python -m nl2pbip.example_run` against a clean workspace
# via [asciinema], then renders the captured .cast into both a GIF
# (assets/demo.gif) and an MP4 (assets/demo.mp4) via [agg] + ffmpeg.
#
# Required binaries (downloadable, no brew required):
#   * ~/.hermes/bin/asciinema — `asciinema-aarch64-apple-darwin`
#     from https://github.com/asciinema/asciinema/releases
#   * ~/.hermes/bin/agg        — `agg-aarch64-apple-darwin`
#     from https://github.com/asciinema/agg/releases
#   * ~/.hermes/tools/ffmpeg-* — already vendored for nl2pbip
#
# Re-run this script after a pipeline change to refresh the demo
# assets committed to git. The README references the assets via
# the relative paths `assets/demo.{gif,mp4}`.
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

# Clean any prior run state so the recording starts fresh.
rm -rf artifacts/report_workspace.json
rm -rf artifacts/semantic_workspace
rm -rf artifacts/SalesInsights.pbipdir

# Record. cols/rows match the recording at 130×36 (HD-ready). The
# --idle-time-limit caps long pauses from the demo script.
TERM=xterm-256color "$ASCIINEMA" rec --quiet \
    --idle-time-limit 2.5 \
    --cols 130 --rows 36 \
    --command "bash assets/demo_script.sh" \
    assets/demo.cast

# Render to GIF (the asset that lives in the README).
"$AGG" assets/demo.cast assets/demo.gif \
    --theme dracula --font-size 14 --cols 130 --rows 36

# Convert the GIF → MP4 (H.264 + faststart for streaming) so users
# who follow the GitHub-embedded link get a proper video.
"$FFMPEG" -y -loglevel error \
    -i assets/demo.gif \
    -movflags +faststart \
    -pix_fmt yuv420p \
    -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
    assets/demo.mp4

ls -la assets/demo.{cast,gif,mp4}
echo "OK — refresh README preview to verify."