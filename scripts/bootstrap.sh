#!/bin/bash
# Install user-scoped tools and Docker Desktop on an Apple Silicon Mac.
set -euo pipefail
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || {
    echo 'This bootstrap requires Apple Silicon macOS.' >&2; exit 1;
}
repo_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
tools_dir="$HOME/.local/share/mac-studio-agents"
mkdir -p "$tools_dir/downloads" "$HOME/.local/bin"
chmod 700 "$tools_dir"
export PATH="$HOME/.local/bin:$tools_dir/tools/bin:$HOME/.docker/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    curl -fsSL --retry 3 https://astral.sh/uv/install.sh -o "$tools_dir/downloads/install-uv.sh"
    UV_NO_MODIFY_PATH=1 bash "$tools_dir/downloads/install-uv.sh"
fi
uv venv --python 3.12 "$tools_dir/ops-venv" --allow-existing

# The system git shim requires Command Line Tools. Use it when available;
# otherwise install a user-scoped Git without requesting an admin password.
if ! git --version >/dev/null 2>&1; then
    curl -fsSL --retry 3 https://micro.mamba.pm/api/micromamba/osx-arm64/latest \
        -o "$tools_dir/downloads/micromamba.tar.bz2"
    tar -xjf "$tools_dir/downloads/micromamba.tar.bz2" -C "$tools_dir/downloads" bin/micromamba
    "$tools_dir/downloads/bin/micromamba" create -y -p "$tools_dir/tools" -c conda-forge git
    ln -sf "$tools_dir/tools/bin/git" "$HOME/.local/bin/git"
fi

if [[ ! -d /Applications/Docker.app ]]; then
    curl -fsSL --retry 3 https://desktop.docker.com/mac/main/arm64/Docker.dmg \
        -o "$tools_dir/downloads/Docker.dmg"
    hdiutil attach "$tools_dir/downloads/Docker.dmg" -nobrowse -readonly
    trap 'hdiutil detach /Volumes/Docker >/dev/null 2>&1 || true' EXIT
    ditto /Volumes/Docker/Docker.app /Applications/Docker.app
    codesign --verify --deep --strict /Applications/Docker.app
    hdiutil detach /Volumes/Docker
    trap - EXIT
fi
open -a Docker --args --accept-license --unattended

uv venv --python 3.12 "$HOME/.venv-vllm-metal" --allow-existing
uv pip sync --python "$HOME/.venv-vllm-metal/bin/python" "$repo_dir/requirements-metal.lock"
echo 'Installed. Download the model with scripts/download-model.sh, then run scripts/msa init.'

