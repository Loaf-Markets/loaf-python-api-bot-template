#!/usr/bin/env bash
# Runs the full-stack perps e2e (README.md) on the box that hosts the stack.
# Usage: run.sh <sdk tree> <log file>, with the E2E_* environment set.
# The venv (E2E_VENV, default .venv here) is made once from lock.txt: Python 3.12 (E2E_PYTHON), hash-pinned wheels
# only, no dependency resolution; the SDK tree goes on its path by a .pth file, so nothing is built.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
sdk=$(cd "${1:?usage: run.sh <sdk tree> <log file>}" && pwd)
out=${2:?usage: run.sh <sdk tree> <log file>}
venv=${E2E_VENV:-$here/.venv}
python=${E2E_PYTHON:-python3.12}

[ -f "$sdk/loaf/k1.py" ] || { echo "run.sh: $sdk is no SDK tree (no loaf/k1.py)" >&2; exit 2; }
if [ ! -x "$venv/bin/python" ]; then
    "$python" -m venv "$venv"
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: \
        --no-deps -r "$here/lock.txt"
fi
site=$("$venv/bin/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
printf '%s\n' "$sdk" > "$site/loaf-sdk-e2e.pth"
"$venv/bin/python" -I -c 'import loaf, sys; print("sdk", loaf.__file__, "python", sys.version.split()[0])'

set +e
"$venv/bin/python" -I "$here/e2e.py" 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
echo "run.sh: e2e.py exit $rc" | tee -a "$out"
exit "$rc"
