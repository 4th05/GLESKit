# Check source-release scope

The GitHub deliverable contains source, branding, documentation, tests and pinned
Docker build instructions. It excludes installed runtimes, prebuilt images,
personal profiles, keys, sessions, databases and research outputs.

## Contents

- [Review source before publishing](#source-preparation-controls)
- [Check validation coverage](#runtime-validation-boundary)
- [Identify work outside this release](#what-this-source-release-does-not-approve)

## Source preparation controls

Before publishing:

1. Run the [release checks](../scripts/release/README.md).
2. Review contents, image metadata, Git history and author identity. Use the
   reviewed fresh source repository, not private development history.
3. Keep `.env`, `var/`, local configuration, sessions and unpublished research
   outside source/image inputs. The default-deny Docker context enforces this
   boundary; synthetic canaries test the actual context.
4. Review changed bytes again. `publication.py` checks hashes and file sets;
   private files cannot be renamed into an export.
5. Retain the standard license and upstream notices. The source record in
   `docs/licensing-review.json` is technical review, not legal certification.

Brand PNG/SVG account metadata was removed without changing decoded pixels.
Regression checks cover source assets and image layers. The clean history uses a
generic author and imports no development Git objects, account email, hooks or
remote credentials.

## Runtime validation boundary

Tests use native Linux ARM64 under Apple Silicon Docker Desktop, fresh state,
synthetic providers/data and real pinned OpenCode.

| Covered | Checks |
| --- | --- |
| HTTP | Authentication, Host/Origin, CSRF, private child ports |
| Chat | Instructions, protocols, key redaction, uploads and figure downloads |
| Science | Locked R namespaces; synthetic ORA/GSEA/program comparison/DESeq2/apeglm; headless graphics |
| State/lifecycle | Settings/chat/uploads, restart and scoped graceful/abrupt shutdown |
| Build contents | Context canaries, image history/layers, personal-path markers and brand metadata |

The app probe uses an internal network; protocol fixtures have no external
network. Docker Desktop HTTP checks use container loopback while host-only
mappings are inspected separately. Native/launcher tests cover the browser handoff.

These checks are bounded, not complete syscall-level network observation or OS
certification. `network_observation_gate_passed` remains false. Configured remote
providers still receive intentionally submitted requests.

## What this source release does not approve

- **Prebuilt images/dependencies:** review source/relinking, license-text and
  notice obligations before opening the separate image-distribution gate.
- **Public MCP/ngrok:** authenticated remote deployment is not implemented.
- **Other platforms:** Intel, emulation, Windows and other hosts need validation.
- **Research outputs:** notebooks, raw data and paper results remain separate.
- **Automatic publication:** no tool/CI step pushes repositories, publishes images
  or uploads private reports. Manual CI is read-only and has no model secrets.

Keys are plaintext in a private `0600` profile `.env`. Keep volumes/backups private;
see [credential handling](privacy.md). The relay does not modify or certify
OpenCode's own code.
