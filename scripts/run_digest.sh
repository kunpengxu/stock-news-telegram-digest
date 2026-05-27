#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# Optional local env file for launchd/non-interactive runs.
if [[ -f "${PROJECT_DIR}/.env.local" ]]; then
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env.local"
fi

PYTHON_BIN="${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.11)"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "[runner] No usable python interpreter found." >&2
    exit 1
  fi
fi

# If enabled, gate execution to Beijing 09:00 and 18:00 windows.
if [[ "${ENABLE_BEIJING_GATE:-true}" == "true" ]]; then
  BJ_HOUR="$(TZ=Asia/Shanghai date +%H)"
  BJ_MINUTE="$(TZ=Asia/Shanghai date +%M)"
  BJ_DATE="$(TZ=Asia/Shanghai date +%F)"

  SHOULD_RUN="false"
  SLOT=""
  if [[ "${BJ_HOUR}" == "09" && "${BJ_MINUTE}" -lt 10 ]]; then
    SHOULD_RUN="true"
    SLOT="morning"
  elif [[ "${BJ_HOUR}" == "18" && "${BJ_MINUTE}" -lt 10 ]]; then
    SHOULD_RUN="true"
    SLOT="evening"
  fi

  if [[ "${SHOULD_RUN}" != "true" ]]; then
    echo "[runner] Skip: outside Beijing run window (now BJ ${BJ_HOUR}:${BJ_MINUTE})."
    exit 0
  fi

  # Prevent duplicate runs in the same Beijing slot.
  STATE_FILE="${PROJECT_DIR}/.last_run_${BJ_DATE}_${SLOT}"
  if [[ -f "${STATE_FILE}" ]]; then
    echo "[runner] Skip: already ran for ${BJ_DATE} ${SLOT}."
    exit 0
  fi
fi

cd "${PROJECT_DIR}"
"${PYTHON_BIN}" main.py

if [[ "${ENABLE_BEIJING_GATE:-true}" == "true" ]]; then
  BJ_DATE="$(TZ=Asia/Shanghai date +%F)"
  BJ_HOUR="$(TZ=Asia/Shanghai date +%H)"
  SLOT="unknown"
  if [[ "${BJ_HOUR}" == "09" ]]; then
    SLOT="morning"
  elif [[ "${BJ_HOUR}" == "18" ]]; then
    SLOT="evening"
  fi
  touch "${PROJECT_DIR}/.last_run_${BJ_DATE}_${SLOT}"
  find "${PROJECT_DIR}" -maxdepth 1 -name '.last_run_*' -type f -mtime +7 -delete || true
fi
