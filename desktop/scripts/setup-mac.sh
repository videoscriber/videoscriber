#!/bin/bash
# First-run setup for the Videoscriber Mac desktop app.
# Creates a per-user Python venv at ~/Library/Application Support/Videoscriber,
# installs the Python backend requirements, and prompts for API keys.
#
# Usage (after installing the .dmg into /Applications):
#   /Applications/Videoscriber.app/Contents/Resources/scripts/setup-mac.sh

set -e

SUPPORT_DIR="${HOME}/Library/Application Support/Videoscriber"
VENV_DIR="${SUPPORT_DIR}/venv"
ENV_FILE="${SUPPORT_DIR}/.env"

# Resolve the bundled requirements.txt. When run from inside the .app bundle,
# the requirements file sits next to this script's parent (Contents/Resources).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SCRIPT_DIR}/../requirements.txt"
if [ ! -f "${REQ_FILE}" ]; then
    # Fallback: repo layout (desktop/scripts/setup-mac.sh)
    REQ_FILE="${SCRIPT_DIR}/../../requirements.txt"
fi
if [ ! -f "${REQ_FILE}" ]; then
    echo "ERROR: could not locate requirements.txt next to this script." >&2
    exit 1
fi

echo "=== Videoscriber desktop setup ==="
echo "Support dir: ${SUPPORT_DIR}"
echo ""

# --- Prereq checks ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.10+:" >&2
    echo "  brew install python@3.12" >&2
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
PY_MAJOR=$(echo "${PY_VERSION}" | cut -d. -f1)
PY_MINOR=$(echo "${PY_VERSION}" | cut -d. -f2)
if [ "${PY_MAJOR}" -lt 3 ] || { [ "${PY_MAJOR}" -eq 3 ] && [ "${PY_MINOR}" -lt 10 ]; }; then
    echo "ERROR: Python ${PY_VERSION} is too old. Need 3.10+." >&2
    exit 1
fi
echo "Python ${PY_VERSION} found."

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ERROR: ffmpeg not found. Install:" >&2
    echo "  brew install ffmpeg" >&2
    exit 1
fi
echo "ffmpeg found: $(ffmpeg -version | head -1)"
echo ""

# --- Create/refresh venv ---
mkdir -p "${SUPPORT_DIR}"
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating venv at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

echo "Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${REQ_FILE}" --quiet
echo "Dependencies installed."
echo ""

# --- API keys ---
if [ ! -f "${ENV_FILE}" ]; then
    echo "=== API keys ==="
    echo "Videoscriber needs two API keys. They stay on this machine only."
    echo ""
    read -r -p "OpenAI API key (sk-...): " OPENAI_KEY
    read -r -p "AssemblyAI API key: " ASSEMBLYAI_KEY
    cat > "${ENV_FILE}" <<EOF
OPENAI_API_KEY=${OPENAI_KEY}
ASSEMBLYAI_API_KEY=${ASSEMBLYAI_KEY}
EOF
    chmod 600 "${ENV_FILE}"
    echo "Saved API keys to ${ENV_FILE}"
else
    echo "API keys already configured at ${ENV_FILE} (edit that file to change them)."
fi

echo ""
echo "=== Setup complete ==="
echo "Launch Videoscriber from /Applications."
