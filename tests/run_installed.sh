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
"$V/python" - <<'PY'
# the installed app must serve its own page, which is package data
import http.client, threading, tempfile, surya_distro
from surya_distro import app
assert "site-packages" in app.__file__, app.__file__
d = app.Distro(tempfile.mkdtemp())
httpd = app.make_server(d)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=10)
c.request("GET", "/", headers={"Host": f"127.0.0.1:{d.port}"})
r = c.getresponse(); body = r.read().decode()
assert r.status == 200 and "Surya Distro" in body and d.nonce in body, r.status
httpd.shutdown()
print("installed copy serves the app page")
PY
echo "installed copy: all checks passed"
