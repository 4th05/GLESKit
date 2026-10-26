# Run release checks

Use Python 3.11+ from the project root. Source checks do not start the app or
publish anything. Docker validation is an explicit disposable synthetic test.

## Contents

- [Check source, assets and licenses](#check-source-assets-and-licenses)
- [Review and export source](#review-and-export-source)
- [Validate Docker](#opt-in-docker-validation)
- [Diagnose unavailable upstream sources](#diagnose-unavailable-upstream-sources)
- [Check legacy runtime evidence](#check-legacy-runtime-evidence)
- [Run CI manually](#run-ci-manually)

## Check source, assets and licenses

```sh
python3 scripts/release/privacy_gate.py --report tmp/source-privacy.json
python3 scripts/release/asset_metadata.py
python3 scripts/release/licensing_gate.py --distribution source
python3 scripts/release/publication.py --verify-boundary
```

| If a check fails | Do this |
| --- | --- |
| Privacy/history findings | Inspect rules/locations in the private report; review historical bytes too |
| Image metadata | Review the asset; `asset_metadata.py --sanitize` scrubs only the named public PNG/SVG assets losslessly |
| Licensing mismatch | Review changed inputs and refresh exact-byte records; tests alone do not approve them |
| Publication mismatch | Review changed files and renew content review; do not bypass the guard |

Reports show rules/locations, never matched credentials. Ignored runtime state is
not scanned wholesale or uploaded. A passing scan does not establish that arbitrary
research data is publishable. Image distribution has a separate
`licensing_gate.py --distribution image` check.

With development dependencies installed:

```sh
PYTHONPATH=src .venv/bin/python -m unittest tests.test_release_privacy tests.test_release_publication tests.test_release_runtime_privacy tests.test_release_licensing -v
```

## Review and export source

For a development checkout with private history:

1. Generate a private committed-file inventory:

   ```sh
   python3 scripts/release/publication.py --report tmp/source-inventory.json
   ```

2. Review each included/replacement file and record explicit exact-byte decisions.
   An inventory is not approval; private/research paths cannot enter an export.
3. Export the completed manifest to a **new sibling directory**, outside the
   development checkout (replace the target if necessary):

   ```sh
   python3 scripts/release/publication.py --manifest tmp/reviewed-source-manifest.json --export ../GLESKit-publication
   ```

4. Initialize fresh Git history there with a deliberate public author identity.
   Add files and repeat privacy, license, asset and boundary checks.
5. Set a remote and publish only as a separate authorized action. Never import
   private development history into the clean repository.

Changed commits/hashes invalidate a manifest. Existing clean source repositories
need renewed review for edits, not another nested copy.

## Opt-in Docker validation

Start a local Linux ARM64 engine (28+) with Compose/Buildx. Use a new report path:

```sh
python3 scripts/release/docker_smoke.py --context desktop-linux --build --report tmp/docker-validation.json
```

The runner checks a history-free context and installed source, probes fresh
UI/chat/uploads/figures, imports locked R packages, runs synthetic analyses,
tests persistence/shutdown and scans image layers. It uses no real keys, paid
calls, author profiles or real cohorts. Only uniquely named test resources are
removed; normal volumes, unrelated containers and build caches are preserved.
Private reports/diagnostics are not uploaded.

The app network is internal; protocol fixtures use network-none. Docker Desktop
HTTP checks use container loopback; host-only publication is inspected separately.
This is not full syscall-level network observation:
`network_observation_gate_passed` and `release_approved` remain false.
Functional success does not approve legal/content/publication scope.

## Diagnose unavailable upstream sources

For an explicit app-only test on a matching existing local runtime:

```sh
python3 scripts/release/docker_smoke.py --context desktop-linux --build --reuse-validated-runtime --report tmp/docker-app-validation.json
```

Locks are verified; only app/frontend layers are rebuilt. The report records
`fresh_runtime_rebuild_passed: false`, so this cannot validate a first installation.
Retry the complete build after upstream recovery; do not change pinned versions
just to pass the diagnostic.

## Check legacy runtime evidence

`runtime_privacy_gate.py` checks sealed `ready.json` plus `runtime/opencode`
evidence. Missing coverage, changed identities or incomplete network observation
fail closed. It does not generate evidence or certify the current app as ready.
See [scope and limits](../../docs/release-privacy-status.md).

## Run CI manually

The manual workflow runs source guards first. Enable its optional Docker input
for the native ARM64 build/smoke test. Its pinned checkout is read-only, retains
no credentials, accepts no model secrets and publishes nothing.
