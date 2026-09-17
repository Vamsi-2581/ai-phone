#!/usr/bin/env bash
# scripts/install.sh — get a clone of ai-phone ready to run: the SIP library and the Python environment.
# Safe to run again. It never touches ~/.ai-phone and downloads no models (`uv run ai-phone setup` does that).
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || fail "ai-phone runs on macOS on Apple Silicon"
command -v brew >/dev/null 2>&1 || fail "Homebrew is required: https://brew.sh"
command -v uv >/dev/null 2>&1 || fail "uv is required: brew install uv (or see https://docs.astral.sh/uv/)"

# the SIP library build (native/pjsua2/build.sh) needs swig, pkg-config (pkgconf) and the pjproject headers and libraries;
# the pjproject formula also brings pjsua, the softphone used for free test calls
step "Installing swig, pkgconf and pjproject with Homebrew"
for formula in swig pkgconf pjproject; do
  if brew list --formula "$formula" >/dev/null 2>&1; then echo "$formula: already installed"; else brew install "$formula"; fi
done

step "Setting up the Python environment"
uv sync

step "Building the SIP library"
if uv run python -c "import ai_phone.vendor.pjsua2" >/dev/null 2>&1; then
  echo "already built"
else
  PYTHON="$(uv run python -c 'import sys; print(sys.executable)')" ./native/pjsua2/build.sh
  mkdir -p src/ai_phone/vendor
  cp native/pjsua2/build/pjsua2.py native/pjsua2/build/_pjsua2*.so src/ai_phone/vendor/
  uv run python -c "import ai_phone.vendor.pjsua2" || fail "the SIP library was built but does not import; see native/pjsua2/README.md"
fi

step "Ready"
echo "Next: uv run ai-phone setup"
