#!/usr/bin/env bash
# startup_gate.sh - fail-closed immutability preflight for the Antigona live stack.
#
# systemd runs this as ``ExecStartPre=`` for every Antigona unit, so a service can
# only start against a candidate whose C11 deployment manifest still verifies.
# The gate is DEFAULT-CLOSED: a missing interpreter, a missing validator module or
# any non-zero validator exit aborts the unit start (exit 1).  There is exactly one
# override, and it is loud: ``ANTIGONA_SKIP_STARTUP_GATE=1``.
#
# Usage:
#   scripts/startup_gate.sh
#
# Environment:
#   ANTIGONA_GATE_ROOT      candidate root  (default: derived from this script's path)
#   ANTIGONA_GATE_PYTHON    interpreter     (default: $ANTIGONA_GATE_ROOT/.venv/bin/python)
#   ANTIGONA_GATE_CHECK     validator phase (default: manifest)
#   ANTIGONA_SKIP_STARTUP_GATE=1   explicit operator override: skip the gate
#
# B32: naming an interpreter was never verification.  Before the validator runs,
# the gate now probes that $PY is real Python AND can import the validator module
# from this candidate root; a stub named ``python3`` that only echoes a success
# line is refused (fail-closed) instead of faking a verified contract.
set -uo pipefail

_self="$(readlink -f "${BASH_SOURCE[0]:-$0}")"
_self_dir="$(cd "$(dirname "$_self")" && pwd -P)"
ROOT="${ANTIGONA_GATE_ROOT:-$(cd "$_self_dir/.." && pwd -P)}"
PY="${ANTIGONA_GATE_PYTHON:-$ROOT/.venv/bin/python}"
CHECK="${ANTIGONA_GATE_CHECK:-manifest}"
VALIDATOR_MODULE="antigona.startup.validator"
VALIDATOR_FILE="$ROOT/src/antigona/startup/validator.py"

# ── Single sanctioned override (loud, explicit, exit 0) ───────────────────────
if [[ "${ANTIGONA_SKIP_STARTUP_GATE:-}" == "1" ]]; then
    echo "################################################################" >&2
    echo "# !!! WARNING !!! ANTIGONA_SKIP_STARTUP_GATE=1 !!! WARNING !!!" >&2
    echo "# The C11 deployment-immutability preflight is DISABLED." >&2
    echo "# This service is starting WITHOUT a verified immutability" >&2
    echo "# contract.  Any post-freeze drift in the candidate is now" >&2
    echo "# undetected.  Owner decision required; do not set this env" >&2
    echo "# variable in a production EnvironmentFile." >&2
    echo "################################################################" >&2
    exit 0
fi

# A validator-level bypass (ANTIGONA_SKIP_VALIDATOR=1, honoured by the validator
# itself: it prints "пропущен" and returns 0) must not silently defeat the unit
# contract.  Refuse loudly instead: the only way to skip is the gate override
# above, which is visible in the journal.
if [[ "${ANTIGONA_SKIP_VALIDATOR:-}" == "1" ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - ANTIGONA_SKIP_VALIDATOR=1 is set," >&2
    echo "CRITICAL: which would silently disable the immutability check. Refusing." >&2
    echo "CRITICAL: remove it from the unit environment, or override the gate" >&2
    echo "CRITICAL: explicitly and audibly with ANTIGONA_SKIP_STARTUP_GATE=1." >&2
    exit 1
fi

# ── Fail-closed preflight: never a silent skip ────────────────────────────────
if [[ ! -x "$PY" ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - no executable interpreter at: $PY" >&2
    echo "CRITICAL: cannot verify the deployment immutability contract; refusing to" >&2
    echo "CRITICAL: start the service (set ANTIGONA_GATE_PYTHON or fix $ROOT/.venv)." >&2
    exit 1
fi

# N1: the interpreter must be a real, verifiable Python interpreter.  An override
# such as ANTIGONA_GATE_PYTHON=/bin/true is executable, exits 0 and prints nothing,
# so the gate would report a "verified" contract while verifying nothing.  The
# override is meant to point at a Python (e.g. another venv), never at a silent
# no-op; refuse loudly (fail-closed) instead of faking a pass.
PY_BASENAME="$(basename "$PY")"
case "$PY_BASENAME" in
    python|python[0-9]*|pypy*)
        ;;
    *)
        echo "CRITICAL: startup gate FAIL-CLOSED - ANTIGONA_GATE_PYTHON does not name" >&2
        echo "CRITICAL: a Python interpreter: $PY ('$PY_BASENAME' is not python/pypy)." >&2
        echo "CRITICAL: a non-Python override (e.g. /bin/true) exits 0 without checking" >&2
        echo "CRITICAL: anything and would fake a verified immutability contract." >&2
        echo "CRITICAL: point ANTIGONA_GATE_PYTHON at a Python interpreter or unset it." >&2
        exit 1
        ;;
esac

if [[ ! -f "$VALIDATOR_FILE" ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - validator module missing: $VALIDATOR_FILE" >&2
    echo "CRITICAL: cannot verify the deployment immutability contract; refusing to" >&2
    echo "CRITICAL: start the service." >&2
    exit 1
fi

if ! cd "$ROOT"; then
    echo "CRITICAL: startup gate FAIL-CLOSED - cannot enter candidate root: $ROOT" >&2
    exit 1
fi

# ── B32: the interpreter must be REAL Python and must reach the validator ─────
#
# The basename rule above only proves that the *name* looks like Python.  A stub
# (a two-line shell script called ``python3`` that prints "immutability contract
# verified" and exits 0) satisfies it, exits 0 and even produces output, so the
# gate used to report a verified contract while verifying nothing — the same
# defect class (N1) this gate claims to close, reproduced by two independent
# actors.  Fail closed on what the interpreter can actually DO:
#
#   1. version probe - it must execute real Python and identify itself;
#   2. import probe  - it must be able to import the validator module from THIS
#      candidate root, which is what the actual verification below needs.
#
# The import probe runs after ``cd "$ROOT"`` and prepends ``$ROOT/src`` to
# ``PYTHONPATH``: the prepend is additive, so a normal venv that already ships the
# package resolves exactly as before, while an uninstalled checkout (src layout)
# is still importable.  There is deliberately NO fallback to a "python3 from PATH"
# and no rule below may ever turn these probes into a green exit without a real
# verification; every mismatch is a loud fail-closed refusal.
PY_PROBE_MARKER="ANTIGONA_GATE_PY_PROBE"
PY_IMPORT_MARKER="ANTIGONA_GATE_VALIDATOR_IMPORT_OK"

probe_version_out="$("$PY" -c 'import sys; print("ANTIGONA_GATE_PY_PROBE py%d.%d" % sys.version_info[:2])' 2>&1)"
probe_version_rc=$?
if [[ "$probe_version_rc" -ne 0 ]] \
    || [[ -z "${probe_version_out//[[:space:]]/}" ]] \
    || [[ "$probe_version_out" != *"$PY_PROBE_MARKER"* ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - interpreter is not real Python: $PY" >&2
    echo "CRITICAL: version probe exited $probe_version_rc, output:" >&2
    echo "CRITICAL: ${probe_version_out:0:300}" >&2
    echo "CRITICAL: the basename rule cannot tell Python from a stub that only echoes" >&2
    echo "CRITICAL: a success line; refusing to report a verified immutability" >&2
    echo "CRITICAL: contract.  Point ANTIGONA_GATE_PYTHON at a real interpreter." >&2
    exit 1
fi

probe_import_out="$(cd "$ROOT" && PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c 'import antigona.startup.validator; print("ANTIGONA_GATE_VALIDATOR_IMPORT_OK")' 2>&1)"
probe_import_rc=$?
if [[ "$probe_import_rc" -ne 0 ]] || [[ "$probe_import_out" != *"$PY_IMPORT_MARKER"* ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - interpreter cannot import $VALIDATOR_MODULE: $PY" >&2
    echo "CRITICAL: import probe exited $probe_import_rc, output:" >&2
    echo "CRITICAL: ${probe_import_out:0:300}" >&2
    echo "CRITICAL: without the validator the immutability contract cannot be verified;" >&2
    echo "CRITICAL: refusing to start the service (no fallback interpreter is used)." >&2
    exit 1
fi

output="$("$PY" -m "$VALIDATOR_MODULE" --check="$CHECK" 2>&1)"
rc=$?

# N1 (second half): no-output is no-evidence.  The validator always prints a
# report header, so an interpreter that produced NOTHING verified nothing and must
# not be reported as a verified immutability contract (Security Law 1).
if [[ -z "${output//[[:space:]]/}" ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - interpreter produced no output:" >&2
    echo "CRITICAL: $PY -m $VALIDATOR_MODULE --check=$CHECK" >&2
    echo "CRITICAL: no evidence of a verification run; refusing to report a verified" >&2
    echo "CRITICAL: immutability contract (a silent interpreter is not a pass)." >&2
    exit 1
fi

printf '%s\n' "$output"

if [[ "$rc" -ne 0 ]]; then
    echo "CRITICAL: startup gate FAIL-CLOSED - immutability validation (--check=$CHECK)" >&2
    echo "CRITICAL: exited $rc; the service start is cancelled (ExecStartPre)." >&2
    echo "CRITICAL: single explicit override: ANTIGONA_SKIP_STARTUP_GATE=1" >&2
    exit 1
fi

echo "startup gate: immutability contract verified (--check=$CHECK, root=$ROOT)."
exit 0
