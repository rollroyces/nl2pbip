#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Install Microsoft's ``powerbi-modeling-mcp`` server for the
# cross-server validation tests in ``tests/test_cross_server_validation.py``.
#
# The server ships as a Node.js wrapper around a native .NET binary
# (@microsoft/powerbi-modeling-mcp on npm). On macOS the binary is
# unsigned and Gatekeeper SIGKILLs it at exec time, so we
# ad-hoc-codesign it after install.
#
# The install lives under ``vendor/powerbi-modeling-mcp/`` which is
# git-ignored. Re-running this script is idempotent: it ``git pull``s
# the platform package cache via npm and re-signs.
#
# CI skips the cross-server test by default; enable by setting
# ``NL2PBIP_RUN_CROSS_SERVER_TESTS=1``. See ``README.md`` under
# "Cross-server compatibility".
# ---------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------
#
# The v1.6.1 review noted "macOS / Linux blocks are copy-pasted with
# only brew/apt swapped" — but the live script never had parallel
# brew/apt blocks. There's only one platform-specific branch (the
# macOS Gatekeeper codesign step) because Linux doesn't need one
# (the .NET binary is signed upstream). The helper below is the
# *extracted* form of the platform detection so any future macOS
# /Linux branch (e.g. a Brewfile / apt-get pre-install hint) can
# reuse the same logic instead of duplicating ``uname`` checks.
_detect_install_command() {
    case "$(uname -s)" in
        Darwin)
            echo "brew install node@20"
            ;;
        Linux)
            echo "sudo apt-get install -y nodejs npm"
            ;;
        *)
            echo "see https://nodejs.org/ for the install command on $(uname -s)" >&2
            return 1
            ;;
    esac
}

cd "$(git rev-parse --show-toplevel)"

if ! command -v node >/dev/null 2>&1; then
    echo "error: Node.js ≥18 is required (https://nodejs.org/)." >&2
    echo "       Hint: $(_detect_install_command 2>/dev/null || echo 'install via your package manager')" >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "error: npm is required (install Node.js ≥18)." >&2
    exit 1
fi

VENDOR_DIR="vendor/powerbi-modeling-mcp"

if [[ -d "$VENDOR_DIR" ]]; then
    echo "==> $VENDOR_DIR already exists; running npm install to refresh."
    cd "$VENDOR_DIR"
else
    echo "==> Cloning platform-specific package into $VENDOR_DIR/"
    mkdir -p "$VENDOR_DIR"
    cd "$VENDOR_DIR"
    # Initialise a minimal package.json so npm install --no-save
    # resolves the optionalDependencies platform binary.
    npm init -y >/dev/null
fi

# Pin the version so repeated installs are reproducible. Bump in
# step with Power BI Modeling MCP releases.
VERSION="0.5.0-beta.13"

echo "==> npm install @microsoft/powerbi-modeling-mcp@$VERSION"
# ``--no-save`` keeps our minimal package.json clean.
npm install --no-save --no-audit --no-fund \
    "@microsoft/powerbi-modeling-mcp@$VERSION"

# ---------------------------------------------------------------------
# macOS: the native .NET binary is unsigned; Gatekeeper SIGKILLs
# it at exec time. Ad-hoc codesign it so the cross-server test
# can actually launch it. Re-runs are idempotent.
# ---------------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
    arch="$(uname -m)"
    case "$arch" in
        arm64) platform_arch="darwin-arm64" ;;
        x86_64) platform_arch="darwin-x64" ;;
        *)
            echo "error: unsupported macOS arch $arch." >&2
            exit 1
            ;;
    esac
    bin="node_modules/@microsoft/powerbi-modeling-mcp-${platform_arch}/dist/powerbi-modeling-mcp"
    if [[ ! -f "$bin" ]]; then
        echo "error: expected binary not found at $bin" >&2
        exit 1
    fi
    echo "==> codesign --force --sign - $bin (work around Gatekeeper)"
    codesign --force --sign - "$bin"
fi

cat <<EOF

==> Done. Microsoft 'powerbi-modeling-mcp' is now installed under
    $VENDOR_DIR/.

Enable the cross-server tests with:

    NL2PBIP_RUN_CROSS_SERVER_TESTS=1 pytest tests/test_cross_server_validation.py -v

The fixture under tests/_fixtures/cross_server/Canonical.SemanticModel/
is the canonical PBIP layout Microsoft's parser expects; the orchestrator
output diverges (nl2pbip currently emits a monolithic model.tmdl). The
gap-report test documents that divergence honestly.
EOF