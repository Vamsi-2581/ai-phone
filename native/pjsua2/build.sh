#!/usr/bin/env bash
# native/pjsua2/build.sh — build the pjsua2 Python extension against the Homebrew pjproject bottle.
# Inputs: pjsua2.i and symbols.i (from the pjproject 2.17 source tarball), brew swig + pjproject.
# Output: build/pjsua2.py and build/_pjsua2<EXT_SUFFIX>
set -euo pipefail
cd "$(dirname "$0")"
PJ_PREFIX="$(brew --prefix pjproject)"
export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:$PJ_PREFIX/lib/pkgconfig"
# build for the interpreter that will import the extension: the project environment (or $PYTHON)
PY="${PYTHON:-$(uv python find 2>/dev/null || command -v python3)}"
"$PY" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' || { echo "need Python >=3.11,<3.14 (got $("$PY" -V))" >&2; exit 1; }
PY_INC="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
EXT_SUFFIX="$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
OUT="${OUT:-build}"
mkdir -p "$OUT"
echo "python: $PY"
swig -c++ -python -threads -w312 -I"$PJ_PREFIX/include" -o "$OUT/pjsua2_wrap.cpp" -outdir "$OUT" pjsua2.i
CXXFLAGS="$(pkg-config --static --cflags libpjproject)"
LIBS="$(pkg-config --static --libs libpjproject)"
# keg-only Homebrew libraries that libpjproject.pc names without a search path
for kb in openssl@3 opus; do
  p="$(brew --prefix "$kb" 2>/dev/null || true)"
  [ -n "$p" ] && [ -d "$p/lib" ] && LIBS="-L$p/lib $LIBS"
done
clang++ -std=c++17 -O2 -fPIC -shared -undefined dynamic_lookup \
  $CXXFLAGS -I"$PY_INC" "$OUT/pjsua2_wrap.cpp" $LIBS \
  -o "$OUT/_pjsua2$EXT_SUFFIX"
echo "built: $OUT/_pjsua2$EXT_SUFFIX and $OUT/pjsua2.py"
