# coordkit 0.4.3 (upcoming)

## Upgrade manifest readers before adding orphan batching keys

`[reconciler].orphan_ask_cap` and the related `orphan_batch_lines` key require
**every manifest reader to run coordkit >=0.4.3**. Readers on 0.4.0–0.4.2 reject
these keys as unknown fields;
this includes background services, hooks, and CLI installations sharing the manifest.

Safe order: upgrade all readers to >=0.4.3 first, verify their versions, and only
then add the keys to the shared manifest. Before rolling any reader back to
0.4.0–0.4.2, remove these keys from that manifest.

Orphan batches expose only metadata (kind, ID, original owner, age, and the bus
`subject` field when present), never the orphan body. Digest-only batches earn
delivery receipts only after publication in an hourly digest. Each flush renders
up to 12 queued entries and retains the rest for later flushes, with a remaining
count and inspection commands. A full queue rejects new entries instead of
silently evicting pending findings.
