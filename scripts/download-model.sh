#!/bin/bash
set -euo pipefail
model_id="${1:-mlx-community/gemma-4-31b-it-8bit}"
exec "$HOME/.venv-vllm-metal/bin/python" - "$model_id" <<'PY'
import sys
from huggingface_hub import snapshot_download
print(snapshot_download(sys.argv[1], max_workers=4))
PY

