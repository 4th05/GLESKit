# Configure standalone OpenCode

Use this guide for standalone OpenCode and native benchmark runs. The web app
manages its own private engine configuration; it does not need this setup.

## Contents

- [Set up the links](#set-up-the-links)
- [Find prompts and skills](#find-prompts-and-skills)
- [Edit local configuration](#edit-local-configuration)
- [Run evaluations](#run-evaluations)

## Set up the links

1. Install the project's native `.venv` and OpenCode.
2. Back up existing `~/.config/opencode/` configuration. The script replaces the
   managed config/prompt/skill links and removes selected older agent files.
3. From the project root, run:

   ```sh
   make opencode_setup
   ```

| Live path under `~/.config/opencode/` | Repository source |
| --- | --- |
| `opencode.json` | `docs/opencode/opencode.json` (ignored local file) |
| `prompts` | `docs/opencode/prompts` |
| `skills/gleskit-research-assistant` | Matching folder under `docs/opencode/skills` |
| `skills/gleskit-bixbench-eval` | Matching folder under `docs/opencode/skills` |
| `skills/gleskit-repobench-eval` | Matching folder under `docs/opencode/skills` |

## Find prompts and skills

- `prompts/agents/`: research-assistant and benchmark agent personas.
- `prompts/eval/`: reusable BixBench/RepoBench task templates.
- `skills/`: the research and benchmark skill packs and their references.

These are authored model instructions. Keep their tool contracts intact when
editing user documentation.

## Edit local configuration

Edit the ignored `docs/opencode/opencode.json`. On first setup it is rendered
from `opencode.json.example` with the current checkout/executable paths and mode
`0600`; an existing repository-local config is preserved. Do not commit it or keys.
The live configuration follows the symlink to this file.

Web-app provider keys instead belong in **Settings → Providers & models**, in
that app's separate private profile. See [web settings](../local-web.md#configure-providers-and-models).

## Run evaluations

- [BixBench solve and judge commands](../bixbench_evaluation.md).
- [RepoBench solve and judge commands](../repobench_evaluation.md).
