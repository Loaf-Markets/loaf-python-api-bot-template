#!/usr/bin/env bash
# Runs the full-stack perps e2e (README.md) against the P5 kit's fullstack phase.
# Usage: p5-fullstack.sh <sdk tree> <log file>. P5_KIT names the kit's directory, P5_HEAVY_LOCK the box's
# heavy-job lock (held shared), P5_DRIVER_LOCK the stack's one-driver lock (held exclusively), P5_SCOPE_MEM the
# memory scope; E2E_* set beforehand win over the phase's.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
sdk=${1:?usage: p5-fullstack.sh <sdk tree> <log file>}
out=${2:?usage: p5-fullstack.sh <sdk tree> <log file>}
kit=${P5_KIT:-/mnt/zk/g/final/me-p5-bench}
lock=${P5_HEAVY_LOCK:-/mnt/zk/g/locks/box3-heavy.lock}
run=$kit/run

die() { echo "p5-fullstack.sh: $*" >&2; exit 2; }

[ -f "$kit/fullstack.ok" ] || die "no $kit/fullstack.ok: the fullstack phase is not up"
last=$(grep -v '^[[:space:]]*$' "$kit/round.log" 2>/dev/null | tail -n 1 || true)
case $last in *'==== DONE'*) ;; *) die "$kit/round.log does not end ==== DONE" ;; esac
if pgrep -f "^bash $kit/round.sh" >/dev/null; then die "the kit's round.sh is running"; fi
[ -r "$run/fullstack.env" ] || die "no $run/fullstack.env"

# The phase's P5_* settings: KEY=VALUE lines, a value optionally in one pair of quotes; nothing is evaluated.
while IFS= read -r line || [ -n "$line" ]; do
    case $line in
        P5_*=*)
            key=${line%%=*}
            val=${line#*=}
            [[ $key =~ ^P5_[A-Z0-9_]+$ ]] || continue
            if [[ $val =~ ^\'(.*)\'$ || $val =~ ^\"(.*)\"$ ]]; then val=${BASH_REMATCH[1]}; fi
            export "$key=$val"
            ;;
    esac
done < "$run/fullstack.env"
: "${P5_LB_URL:?fullstack.env names no P5_LB_URL}" "${P5_RPC:?fullstack.env names no P5_RPC}"

# One driver at a time on the stack (this e2e, the browser pass): they share the price control's seq, the backstop
# knob and the funder. An exclusive flock(2) on one file, taken at once or refused, held through every exec below
# until the run ends. Its holder writes the first line, "pid <pid> <who> since <time>".
dlock=${P5_DRIVER_LOCK:-/mnt/zk/g/locks/p5-fullstack-driver.lock}
command -v python3 >/dev/null || die "no python3 to take the driver lock"
exec 9>>"$dlock" || die "cannot open the driver lock $dlock"
got=0
python3 -I -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(3)' || got=$?
if [ "$got" = 3 ]; then
    holder=$(head -n 1 "$dlock" 2>/dev/null || true)
    pid=$(sed -n 's/^pid \([0-9][0-9]*\) .*/\1/p' <<<"$holder")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then holder=" by: $holder"; else holder=""; fi
    die "another driver holds $dlock$holder; one driver at a time on the stack: run again when it ends"
fi
[ "$got" = 0 ] || die "cannot lock $dlock (python3 exit $got)"
printf 'pid %s p5-fullstack.sh (sdk e2e) since %s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$dlock"

lb=${P5_LB_URL%/}
case $lb in */api) ;; *) lb=$lb/api ;; esac
export E2E_API=${E2E_API:-$lb}
export E2E_WS=${E2E_WS:-${P5_LB_WS:-}}
export E2E_RPC=${E2E_RPC:-$P5_RPC}
export E2E_FUNDER=${E2E_FUNDER:-${P5_FUNDER:-}}
export E2E_PRICE_CONTROL=${E2E_PRICE_CONTROL:-${P5_PRICE_CONTROL:-$run/price-control.json}}
export E2E_PRICE_STATUS=${E2E_PRICE_STATUS:-${P5_PRICE_STATUS:-$run/price-status.json}}
export E2E_SETTLESUB_METRICS=${E2E_SETTLESUB_METRICS:-${P5_SETTLESUB_METRICS:-http://127.0.0.1:49476/metrics}}
export E2E_ENGINE_METRICS=${E2E_ENGINE_METRICS:-${P5_ENGINE_METRICS:-http://127.0.0.1:49091/metrics}}
export E2E_ADMIN_API_KEY_FILE=${E2E_ADMIN_API_KEY_FILE:-$run/lb-admin.key}
export E2E_OPERATOR=${E2E_OPERATOR:-sql}
export E2E_HOME_INDEX=${E2E_HOME_INDEX:-1500000}
export E2E_TAG=${E2E_TAG:-p5fs}
if [ -z "${E2E_PSQL:-}" ] && [ -z "${E2E_PSQL_DSN_FILE:-}" ]; then
    if [ -x "$run/psql" ]; then
        export E2E_PSQL=$run/psql
    else
        command -v psql >/dev/null || die "neither $run/psql nor a psql on PATH"
        export E2E_PSQL_DSN_FILE=$run/lb.dsn E2E_JOURNAL_DSN_FILE=${E2E_JOURNAL_DSN_FILE:-$run/journal.dsn}
    fi
fi

# The run's wallets: one seed, made once, kept beside the driver.
seed=${E2E_WALLET_SEED_FILE:-$here/state/wallet.seed}
if [ ! -s "$seed" ]; then
    mkdir -p "$(dirname "$seed")"
    (umask 077 && od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$seed")
fi
export E2E_WALLET_SEED_FILE=$seed

echo "p5-fullstack.sh: engine ${P5_ENGINE_HEAD:-?}, backend ${P5_LB_HEAD:-?}, API $E2E_API, RPC $E2E_RPC"
scope=()
if [ -z "${INVOCATION_ID:-}" ] && command -v systemd-run >/dev/null; then  # a system unit already caps memory; no user bus there
    scope=(systemd-run --user --scope --quiet -p "MemoryMax=${P5_SCOPE_MEM:-8G}" --)
fi
exec flock -s "$lock" ${scope[@]+"${scope[@]}"} "$here/run.sh" "$sdk" "$out"
