#!/usr/bin/env bash
# Install into a fresh virtualenv, then run the tests against that installed copy
# rather than the checkout. Run: bash tests/run_installed.sh
# Pick the interpreter with PYTHON=/path/to/python3 (default: python3).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

"$PY" -m venv "$T/venv"
V="$T/venv/bin"
"$V/python" -m pip install -q --disable-pip-version-check "$ROOT"

mkdir "$T/run"; cp -R "$ROOT/tests" "$T/run/tests"
cd "$T/run"
"$V/python" - <<'PY'
import surya_distro, sys
assert "site-packages" in surya_distro.__file__, surya_distro.__file__
print(f"installed copy: {surya_distro.__file__} (Python {sys.version.split()[0]})")
PY
"$V/python" -m unittest discover -s tests -t .
"$V/surya-distro" --version
"$V/surya-distro" flow | grep -q "Nothing is posted"
"$V/surya-distro" --home "$T/home" schedule | grep -q "Nothing below has been installed"
"$V/python" -m surya_distro --help > /dev/null
echo "installed copy: all checks passed"
