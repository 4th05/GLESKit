#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_root="${repo_root}/docs/opencode"
opencode_home="${HOME}/.config/opencode"

# Machine-specific paths belong only in the ignored local file. Preserve an
# existing configuration exactly; never migrate or print provider secrets.
if [ ! -e "${source_root}/opencode.json" ]; then
  "${repo_root}/.venv/bin/python" - "${repo_root}" <<'PY'
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
folder = root / "docs/opencode"
template = json.loads((folder / "opencode.json.example").read_text())
def render(value):
    if isinstance(value, str):
        return value.replace("@@PROJECT_ROOT@@", str(root)).replace("@@PYTHON_PATH@@", str(root / ".venv/bin/python"))
    if isinstance(value, dict):
        return {render(k): render(v) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v) for v in value]
    return value
target = folder / "opencode.json"
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(render(template), stream, indent=2)
    stream.write("\n")
PY
fi

link_path() {
  local source_path="$1"
  local target_path="$2"
  mkdir -p "$(dirname "${target_path}")"
  if [ -e "${target_path}" ] || [ -L "${target_path}" ]; then
    rm -rf "${target_path}"
  fi
  ln -s "${source_path}" "${target_path}"
}

mkdir -p "${source_root}/prompts" "${source_root}/skills"
mkdir -p "${opencode_home}/skills"

rm -rf \
  "${opencode_home}/agents" \
  "${opencode_home}/gleskit-agent-prompt.md" \
  "${opencode_home}/bixbench-eval-agent-prompt.md" \
  "${opencode_home}/bixbench-eval-prompt.md"

link_path "${source_root}/opencode.json" "${opencode_home}/opencode.json"
link_path "${source_root}/prompts" "${opencode_home}/prompts"
link_path "${source_root}/skills/gleskit-research-assistant" "${opencode_home}/skills/gleskit-research-assistant"
link_path "${source_root}/skills/gleskit-bixbench-eval" "${opencode_home}/skills/gleskit-bixbench-eval"
link_path "${source_root}/skills/gleskit-repobench-eval" "${opencode_home}/skills/gleskit-repobench-eval"

echo "OpenCode links updated:"
printf '  %s -> %s\n' "${opencode_home}/opencode.json" "${source_root}/opencode.json"
printf '  %s -> %s\n' "${opencode_home}/prompts" "${source_root}/prompts"
printf '  %s -> %s\n' "${opencode_home}/skills/gleskit-research-assistant" "${source_root}/skills/gleskit-research-assistant"
printf '  %s -> %s\n' "${opencode_home}/skills/gleskit-bixbench-eval" "${source_root}/skills/gleskit-bixbench-eval"
printf '  %s -> %s\n' "${opencode_home}/skills/gleskit-repobench-eval" "${source_root}/skills/gleskit-repobench-eval"
