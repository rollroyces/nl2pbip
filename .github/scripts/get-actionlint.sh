#!/usr/bin/env bash
#
# Download a pinned version of actionlint into a local scratch dir.
# Used by .github/workflows/actionlint.yml. Caching the binary on
# the runner keeps the workflow fast and avoids downloading on
# every run.
#
# Usage:
#   ACTIONLINT_VERSION=1.7.7 bash .github/scripts/get-actionlint.sh
#
# Emits (to stdout):
#   exe=<absolute path to the downloaded actionlint binary>
#
# Output (GHA workflow step) convention: ``echo "exe=$ABS" >> "$GITHUB_OUTPUT"``
# — we leave that to the caller so this script is reusable.

set -euo pipefail

ACTIONLINT_VERSION="${ACTIONLINT_VERSION:-1.7.7}"
DEST_DIR="${RUNAGENT_TEMP:-${TMPDIR:-/tmp}}/actionlint-${ACTIONLINT_VERSION}"
BIN="${DEST_DIR}/actionlint"

if [[ ! -x "$BIN" ]]; then
    mkdir -p "$DEST_DIR"
    # Two arches we actually care about: linux x86_64 (CI runners)
    # and darwin arm64 (local dev on M-series Mac). actionlint
    # ships per-arch archives; pick by uname.
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)  ASSET="actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" ;;
        Darwin-x86_64) ASSET="actionlint_${ACTIONLINT_VERSION}_darwin_amd64.tar.gz" ;;
        Darwin-arm64)  ASSET="actionlint_${ACTIONLINT_VERSION}_darwin_arm64.tar.gz" ;;
        *) echo "unsupported arch: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
    URL="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/${ASSET}"
    echo "downloading actionlint ${ACTIONLINT_VERSION} from ${URL}" >&2
    TMP=$(mktemp -d)
    curl -fsSL "$URL" -o "$TMP/${ASSET}"
    tar -xzf "$TMP/${ASSET}" -C "$TMP"
    install -m 0755 "$TMP/actionlint" "$BIN"
    rm -rf "$TMP"
fi

echo "exe=${BIN}"

# GitHub Actions workflow context: if $GITHUB_OUTPUT is set,
# also write the value there so downstream steps can read it via
# ``${{ steps.<id>.outputs.exe }}``. We append instead of
# replacing so callers can stack extra ``key=value`` lines.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "exe=${BIN}" >> "$GITHUB_OUTPUT"
fi