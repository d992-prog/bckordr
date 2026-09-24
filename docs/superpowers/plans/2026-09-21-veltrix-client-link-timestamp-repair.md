# Legacy 3x-UI client attachment timestamp repair

The authenticated live client-list API returned HTTP200 success:false. Node-only
classification identified a database scan conversion error in `created_at`.
The single `client_inbounds.created_at` value is stored as text. Pinned 3x-UI's
ClientInbound model requires int64 with `autoCreateTime:milli`. The existing
legacy provisioner unconditionally writes `time.strftime(...)` for that column;
its synthetic normalized-schema fixture incorrectly used a text column.

The owner explicitly approved repairing that one creation timestamp after a
private-copy rehearsal and backup. UUID, URI, expiry, traffic, client policy and
inbound parameters must remain unchanged; no VPN restart or public port change.

## Local prevention

- [x] Add RED regression tests against the actual generated remote helper and
  synthetic SQLite schemas: INTEGER timestamp is Unix milliseconds, old TEXT
  timestamp remains compatible, existing links are never rewritten.
- [x] Use the already-existing schema-aware timestamp default helper for newly
  inserted client links. No automatic repair of historical rows and no change to
  identity, policy, HTTP fallback, or remote execution behavior in this patch.
- [x] Run remote-policy/provisioning/barrier tests, Ruff, independent review and
  parent verification. This local fix is not deployed as an unreviewed full update.

## One-time, separately authorized data repair

- [x] Strict known-hosts SSH and a control worker reservation; verify no conflicting
  domain/maintenance/VPN operations. Read expected owner UUID only from control
  key8/worker15 and use it privately on the node to identify the single attachment.
- [x] Verify the installed model/schema, a single text timestamp in inbound1 and
  exact existing owner identity. Verify UTC node timezone before interpreting the
  legacy timezone-less string. Reject ambiguous format/identity; do not invent a date.
- [x] Create a fresh root-only node backup and private rehearsal copy. In the copy,
  convert the original date to Unix milliseconds. Compare every table/schema and
  prove exactly that one field changed. Do not start a second panel/Xray instance.
- [x] Repeat exact-row compare-and-update under a short SQLite transaction on the
  live DB, with full before/after state comparison. Roll back on any unexpected
  difference. Never restore the entire old DB over new events.
- [x] Read the actual client-list API after commit and check Xray process identity
  unchanged. Export only safe booleans/counts/static errors. Keep the closed backup
  on the node; never export timestamps, token, UUID or raw panel responses.

## Evidence

The new INTEGER/BIGINT regression tests failed before the patch; TEXT compatibility
and no-implicit-repair tests already passed. After the two-line fix, the parent's
remote-policy/provisioning/endpoint-barrier run passed **107 tests in 22.56s**;
focused Ruff passed. Independent source review approved both the local prevention
patch and the one-shot operational repair script before invocation.

Read-only preflight verified the owner attachment, UTC conversion and no previous
repair directory. The authorized operation then verified the closed backup and
private-copy rehearsal, committed exactly one changed field, confirmed every other
row and schema unchanged, and confirmed the Xray PID/start-time identity unchanged.
Private backups live under `/var/lib/veltrix-vpn/control-auth/created-at-repair/`.

The actual API probe subsequently passed authentication, status, settings, inbound
listing and global client listing (one inbound, one client). The complete new
node-local configuration observer was also run using the existing owner URI as
the expected legacy candidate; it returned `matched`, enabled, transport matched,
runtime running. This does not persist an endpoint binding, prove runtime key
application, or authorize mutations. No application release or new port was applied.

The preventative source patch is local until the reviewed application release;
the existing attachment is no longer malformed, and its timestamp will not be
rewritten by the current helper's already-linked path. Public beta remains closed.
