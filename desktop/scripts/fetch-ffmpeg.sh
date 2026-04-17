#!/bin/bash
# Downloads static ffmpeg + ffprobe binaries into desktop/bin/.
# These are bundled into the .app via forge.config.js extraResource.
# Source: evermeet.cx — universal macOS builds (arm64 + x86_64).
#
# Invoked automatically by `npm run package` / `npm run make` via package.json.

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "fetch-ffmpeg: not on macOS — skipping."
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${SCRIPT_DIR}/../bin"
mkdir -p "${BIN_DIR}"

fetch() {
    local name="$1"
    local url="$2"
    local target="${BIN_DIR}/${name}"

    if [ -x "${target}" ]; then
        echo "fetch-ffmpeg: ${name} already present — skipping."
        return
    fi

    echo "fetch-ffmpeg: downloading ${name}..."
    local tmp
    tmp=$(mktemp -d)
    trap "rm -rf '${tmp}'" RETURN
    curl -fsSL -o "${tmp}/${name}.zip" "${url}"
    unzip -q "${tmp}/${name}.zip" -d "${tmp}"
    mv "${tmp}/${name}" "${target}"
    chmod +x "${target}"
    echo "fetch-ffmpeg: installed ${name} at ${target}"
}

fetch ffmpeg  "https://evermeet.cx/ffmpeg/getrelease/zip"
fetch ffprobe "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"

echo "fetch-ffmpeg: done."
