#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if [ -d "${REPO_ROOT}/.venv/bin" ]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
    RUFF="${REPO_ROOT}/.venv/bin/ruff"
    MYPY="${REPO_ROOT}/.venv/bin/mypy"
    PYTEST="${REPO_ROOT}/.venv/bin/pytest"
else
    PYTHON="$(command -v python3 || command -v python)"
    RUFF="$(command -v ruff)"
    MYPY="$(command -v mypy)"
    PYTEST="$(command -v pytest)"
fi

echo "=== Gate 1/4: Ruff ==="
set +e
"${RUFF}" check src/ tests/
RUFF_RC=$?
set -e
echo "Gate 1 (ruff) rc=${RUFF_RC}"
if [ ${RUFF_RC} -ne 0 ]; then
    echo "❌ Gate 1 (ruff) failed with exit code ${RUFF_RC}" >&2
    exit ${RUFF_RC}
fi

echo "=== Gate 2/4: Architecture Guard ==="
set +e
"${PYTHON}" scripts/arch_guard.py
ARCH_RC=$?
set -e
echo "Gate 2 (arch_guard) rc=${ARCH_RC}"
if [ ${ARCH_RC} -ne 0 ]; then
    echo "❌ Gate 2 (arch_guard) failed with exit code ${ARCH_RC}" >&2
    exit ${ARCH_RC}
fi

echo "=== Gate 3/4: Mypy (strict) ==="
set +e
"${MYPY}" --strict --cache-dir=/tmp/wave11_mypy src/antigona
MYPY_RC=$?
set -e
echo "Gate 3 (mypy) rc=${MYPY_RC}"
if [ ${MYPY_RC} -ne 0 ]; then
    echo "❌ Gate 3 (mypy) failed with exit code ${MYPY_RC}" >&2
    exit ${MYPY_RC}
fi

echo "=== Gate 4/4: Pytest (full suite) ==="
ulimit -Sn 1024 || true
unset ANTIGONA_PIN
set +e
"${PYTHON}" -m pytest tests --tb=short -q
PYTEST_RC=$?
set -e
echo "Gate 4 (pytest) rc=${PYTEST_RC}"
if [ ${PYTEST_RC} -ne 0 ]; then
    echo "❌ Gate 4 (pytest) failed with exit code ${PYTEST_RC}" >&2
    exit ${PYTEST_RC}
fi

echo "=========================================="
echo "✅ All 4 CI gates passed successfully!"
echo "=========================================="
exit 0
