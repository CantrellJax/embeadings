# Synthetic report examples

These fixtures show the stable, redacted shape of emBEADings schema-v1 artifacts. They contain no
real tracker text, repository paths, credentials, or model output. Use them when building a consumer
or reviewing the privacy boundary; use the installed CLI for current command help.

| Example | Purpose |
| --- | --- |
| [`triage.json`](triage.json) | Compact bounded packet intended for routine human or agent review |
| [`collisions.json`](collisions.json) | Exact-file lead from two genuine-worktree-style synthetic pointers |
| [`neighbors.json`](neighbors.json) | Semantic neighbors for one issue |
| [`neighbors-batch.json`](neighbors-batch.json) | Semantic neighbors for several seeds sharing one load |
| [`mentions.json`](mentions.json) | Text lineage claim with no typed link, reported without the text |
| [`orphans.json`](orphans.json) | Live issues whose parent is closed or missing, plus optional parentless groups |
| [`match.json`](match.json) | Nearest existing records for candidate text that is not yet a bead |
| [`superseded.json`](superseded.json) | Live records that merged changes name or closely match |
| [`sweep.json`](sweep.json) | Full synchronous analysis and batching report |
| [`batch.json`](batch.json) | Compatibility alias for a synchronous sweep artifact |
| [`capabilities.json`](capabilities.json) | Schema and feature negotiation handshake |
| [`checkpoint.json`](checkpoint.json) | Metadata-only incremental review checkpoint |

Machine consumers should validate reports against the matching files under
[`schemas/v1`](../schemas/v1/) and follow the
[consumer compatibility contract](../docs/consumer-contract.md). Example scores are illustrative;
they are not model benchmarks or recommended thresholds.
