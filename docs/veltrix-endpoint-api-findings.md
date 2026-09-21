# Protected-endpoint adapter: source findings

Date: 2026-09-21. Preparatory source review, **not a live API acceptance test**.
The preceding read-only node inspection reported 3x-UI 3.8.5 and Xray 26.9.9.
This review made no node calls, created no panel session/token and changed no setting.

## API identity and scope

The version-pinned client controller exposes `/clients/add`, `/clients/update/:email`,
`/clients/bulkDisable`, `/clients/del/:email` and `/clients/:email/detach` under the
panel API prefix. An implementation must follow this version's request schemas,
not assume older inbound-scoped `addClient`/`delClient` routes. See the
[3.8.5 client controller](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/controller/client.go).

An inbound filter on update does not isolate every field: the service also updates
the global client record, including enable and other settings. Credential omission
has preservation logic, but that does not establish patch semantics for every
field. Global deletion and selective detachment are different operations. Inspect
the complete verified record and all its attachments before any mutation; never
infer identity from email alone. See the
[3.8.5 client service](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/client_crud.go).

## Restart and effective revocation remain a release gate

`RestartXray(false)` first attempts a hot application, but failure can lead to
stopping and starting the shared process. The hot path also deliberately declines
user removal when the restart-on-disable setting requires dropping live sessions.
Therefore changing that setting to false would not prove either a no-restart
guarantee or complete immediate revocation. See
[3.8.5 Xray reconciliation](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/xray.go).

In Xray 26.9.9, VLESS `RemoveUser` removes the credential from the validator (and
handles reverse-proxy state); it does not explicitly close ordinary existing
connections. The panel source also documents this distinction. The inference for
our acceptance tests is that failure of a **new** login alone is insufficient:
already-authenticated traffic must be tested separately. See the
[26.9.9 VLESS handler](https://raw.githubusercontent.com/XTLS/Xray-core/v26.9.9/proxy/vless/inbound/inbound.go).

The original design required both effective suspension/revocation and no routine
interruption of another client's control connection. The owner has now accepted
shared reconnections on disable/expiry for the ten-person closed beta (see below).
A plain API wrapper still does not prove effective removal or successful recovery.
Do not silently extend the exception, patch/replace the installed panel, change
its settings, or declare a best-effort hot update sufficient.

## Current safe increment

The recorded-endpoint resolver and legacy mutation barrier are local intermediate
work. Bound profiles must stay pending with a static diagnostic until the adapter
and durable intents exist. Existing unbound behavior is temporary compatibility,
not the final migration policy. Do not deploy this checkpoint or create production
endpoint bindings. Next work also needs verified legacy import, strict SSH trust,
remote identity checks, and real PostgreSQL concurrency/recovery evidence.

## Pinned inventory contract for identity preflight

The next local helper consumes decoded responses without fetching or retaining
raw data. It is configuration observation only; it does not authorize mutation.
The pinned [global list implementation](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_lookup.go#L171)
lists normalized client records without user filtering or pagination. The
[response type](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client.go#L19)
flattens those fields and adds `inboundIds` (possibly null for an orphan).
Record `id` is numeric; `uuid` is the credential. Per-inbound flow can override
the global value, so this identity-only check must not compare global flow.

In contrast, the [inbound list](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/inbound.go#L180)
is scoped to the authenticated panel user. Its stored `settings.clients` may
contain stale identities, while normalized records supply runtime configuration.
Require the exact UUID/email pair, exclusive attachment and consistent enabled
state in both lists. Refuse ambiguity instead of repairing it automatically.
The helper's `not_observed` is deliberately weaker than absence: two non-atomic,
potentially incomplete responses cannot establish safe creation or revocation.

Valid [inbound JSON serialization](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/database/model/model.go#L165)
emits nested settings objects. Malformed saved settings can become strings and
must be rejected by the pinned parser. No generic string-decoding fallback is
needed for this contract. Unknown panel versions require separate validation.
The [mutation pending response](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/util.go#L175)
has `obj: {nodePending: true}` and is not an inventory. Missing this flag in a
list response does not prove the running core matches the panel's database.

The later transport checker must use the actual nested
[REALITY schema](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/frontend/src/schemas/protocols/security/reality.ts#L3):
`realitySettings.serverNames`, `shortIds` and `settings.publicKey/fingerprint`.
Identity comparison alone checks none of these and must not mark an endpoint
ready. A trusted full collector, SSH pin enforcement, complete transport validation,
durable intents and runtime postconditions remain separate release requirements.

## Application integration still required before release

Source audit at `a39dedd` found these caller boundaries; the local barrier does not
claim to replace their current authorization or transaction policy:

- `vpn_provisioning.py` is the shared mutation service for manual admin actions,
  customer archive and lifecycle processing. Its old scripts operate on normalized
  client records/attachments globally. Merely changing a payload's inbound ID is
  not an endpoint-aware implementation.
- `vpn_lifecycle.py` and the admin retry route still use worker-default eligibility
  and node selection. Bound records must later use endpoint-aware eligibility,
  preserve the assigned worker, and refresh locked state before remote actions.
  The existing attack queries in `vpn_policy.py` and `worker_decommission.py`
  include planned/running attack runs but not verifying; extend and test that
  protection before endpoint-aware operations are enabled. The rollout helper's
  broader attack guard is not evidence that every application path has it.
- Subscription synchronization stages desired policy. Manual revoke must remain
  final across retries; callers must not overwrite a newer revoke with stale
  provision/suspend completion.
- Existing `flush()` before SSH is not a durable commit. A later operation-intent
  design must cover a crash after remote success and before result persistence,
  with real independent PostgreSQL connections and bounded recovery.
- Decommission must retain node credentials while bound revocation is incomplete.
  Missing-node/no-URI shortcut branches in lifecycle/archive also need explicit
  endpoint-aware review during final integration, even though valid persisted
  bindings currently require a matching non-null worker through DB constraints.

Do not turn the temporary unbound compatibility path into a permanent fallback.
No control/lifecycle refactor, remote import or endpoint selection was performed
as part of this source audit.

## Follow-up: owner-approved closed-beta disconnect exception

On the next continuation, the parent checked the official
[3.8.5 release notes](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5), which
explicitly describe manual disable/delete as well as expiry/quota disabling under
the restart-on-client-disable setting. They distinguish credential removal from
ending established traffic. This confirms that the prior source finding is an
upstream-described behavior, not merely an inferred application bug.

The upstream [disconnect clarification](https://github.com/MHSanaei/3x-ui/pull/6551)
also explains that the IP-limit helper's temporary credential removal is not a
per-client live-session shutdown; its enforcement relies on fail2ban at the IP
layer. A public-IP ban is not a suitable substitute for our profile-level revoke:
users can share an address behind NAT, and an account can reconnect elsewhere.
No IP ban, panel login, setting change, restart or empirical node test was performed.

The owner explicitly answered: “Да, для закрытого теста допускаем переподключение
(рекомендую)”. This permits shared reconnections on disabling access or subscription
expiry for the first ten-person closed beta; it is not a general restart policy
for public release. The amended design still requires identity preservation,
effective removal of established traffic and recovery of the control profile.
Add/update failure paths may also restart the core; report and test these rather
than promising unconditional continuity. This design choice does not authorize
opening a port or immediately modifying the running node. No reconnection duration
has been measured. Public-release continuity remains a separate design decision.
