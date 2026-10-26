# Protect local data and credentials

Use the controls below when configuring providers, sharing files or connecting
an MCP client. See [release scope](release-privacy-status.md) for distribution checks.

## Contents

- [Store and change keys](#local-state-and-credentials)
- [Check what leaves the computer](#intentional-external-requests)
- [Connect MCP and share figures](#mcp-exposure-and-downloads)
- [Prepare source for sharing](#source-and-release-policy)

## Local state and credentials

- Enter/change web keys through **Settings → Providers & models**. They are
  stored as plaintext in the private profile `.env` (directory `0700`, file `0600`).
  Permissions are not encryption and do not protect against the same OS user.
- Keep that file, runtime profiles, chat logs and volume backups out of Git and
  shared archives. Docker stores them outside image layers.
- A blank password input retains the connection's saved key. Removing the
  connection removes that key and prevents an older Keychain copy reappearing.
- The Python provider relay holds real keys. OpenCode/MCP receive no real provider
  credentials; child environments are explicit. Error redaction is not protection
  against every possible malicious encoding or unrelated sensitive content.
- Manage sharing/update/plugin behavior through the private engine configuration.
  Sharing, discovery, automatic downloads and telemetry exporters are disabled;
  these controls are not an OS firewall.

## Intentional external requests

Before submitting sensitive inputs, check which service receives them:

| Operation | Destination | Submitted information |
| --- | --- | --- |
| Chat | Activated OpenAI, OpenRouter, Anthropic or configured endpoint | Prompt, conversation context and selected tool results |
| Model discovery | Selected provider `/models`; anonymous models.dev metadata for default OpenAI/Anthropic before a key | Model-list request; keys go only to the exact configured provider, never models.dev |
| Gene/protein annotation | MyGene, Ensembl, UniProt, QuickGO | Requested gene/protein/species IDs |
| Enrichment/network evidence | Reactome, STRING, g:Profiler, Enrichr | Gene sets, organisms and analysis options |
| Disease/target/drug evidence | Open Targets, GWAS Catalog, ChEMBL | IDs and query filters |
| Literature | Europe PMC | Search terms and requested articles |
| Expression/cohort evidence | GTEx, Expression Atlas, selected cohort source | Dataset, tissue/species and gene filters |
| Reference/cohort downloads | Selected source; HGNC for gene references | Requested dataset/reference IDs |
| Python/R analysis | Local tools, unless the chosen analysis invokes an external provider | Selected analysis inputs |

Do not submit patient identifiers, keys or data whose external processing is not
authorized. Gene sets and disease/search terms can also be sensitive. Following
article/reference links makes an external browser request. Anonymous model
metadata is not proof of account access.

## MCP exposure and downloads

- Use local stdio or loopback HTTP. `make expose_mcp` is blocked: authenticated
  remote deployment is not implemented. Do not tunnel the unauthenticated MCP or
  companion download listener through ngrok.
- In the web app, open figures using the authenticated UI links.
- Standalone figure links use unpredictable, expiring bearer tokens and expose
  only registered bytes, not arbitrary files. Anyone with a valid token can use
  it until expiry; keep links to confidential plots private.

## Source and release policy

Before sharing source:

1. Exclude private configuration, runtime captures, sessions, credentials,
   notebooks/research outputs and backups from the product export.
2. Review file contents and Git history, including author metadata. Pattern scans
   alone cannot identify every patient ID, unpublished finding or secret format.
3. Run the [release checks](../scripts/release/README.md). Changed bytes invalidate
   their exact-hash review until it is refreshed.
4. Keep prebuilt-image redistribution and authenticated remote deployment outside
   the source-only release unless their separate reviews are completed.

A passing local build or source scan is not complete runtime-network observation,
legal certification or authorization to publish anything automatically.
