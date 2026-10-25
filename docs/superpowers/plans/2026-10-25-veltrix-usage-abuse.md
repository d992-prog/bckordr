# Veltrix Usage and Abuse Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show measured traffic by customer, logical device and node, and surface sustained suspicious source sharing for administrator review without changing client compatibility or automatically blocking access.

**Architecture:** Add a separate bounded read-only `--usage` operation to the existing node zipapp and pinned SSH transport. Collect outside database locks; atomically apply monotonic counter deltas and replace the latest HMAC-only source sample under a short worker/poll lock. Evaluate complete same-minute device evidence across every child credential and create an incident only after five consecutive complete windows with more than two normalized sources.

**Tech Stack:** Existing Python standard library, 3x-UI SQLite counters, node zipapp, Xray CLI, async SQLAlchemy/PostgreSQL, FastAPI and React. No new dependency, monitoring service, destination logs, HWID requirement or external provider.

**Implementation checkpoint:** Tasks 1–5 are implemented with independent specification and quality approval. Actual PostgreSQL migration, lock/lease, acknowledgement and complete-window regressions passed. Statistics and incident review passed mounted browser checks, including out-of-list customer/device navigation, uncertain acknowledgements, delayed editor targets and consecutive subscription extensions. Collection stays disabled by default; DB-only retention continues independently of collection/SSH configuration. Production collection and runner updates remain rollout steps.

Integration addition: capped global admin lists cannot resolve every incident target. The read-only admin `GET /control/vpn/abuse/devices/{device_id}/context?customer_id={id}` returns an owner-constrained customer, subscription and one representative child (or null), independently of those caps. Both IDs are positive int32 and responses are no-store. It adds no revoke API or policy bypass. Successful mutations update the matching retained context without replacing newer versions or a later manual selection.

Hourly aggregates cannot prove uninterrupted period coverage: fresh measured totals are conservatively labelled partial. Resets, outages and the ending-hour boundary must never be hidden by sample-count arithmetic.

---

Implements approved spec §§5–6 after device foundation/location integration. Tasks 1–3 are the read-only traffic slice; Tasks 4–5 add abuse review. Task commits are checkpoints; do not deploy an intermediate schema/protocol without its compatible readers. This plan authorizes no real node update, configuration change, restart or production migration.

## Files and boundaries

| Files | Change |
| --- | --- |
| New `backend/app/services/vpn_node_usage.py` | Dependency-free request/receipt dataclasses, exact parser/serializer and limits |
| New `backend/app/services/vpn_node_usage_entrypoint.py` | Read-only counters/source observer and root entrypoint |
| `backend/app/services/vpn_node_bundle.py`, `vpn_node_deployment.py`; existing bundle/deployment/bootstrap tests | Package/import-probe `--usage` through the current deployment mechanism |
| `backend/app/services/vpn_node_transport.py` | Dedicated fixed-command bounded usage SSH operation |
| `backend/app/services/vpn_xui_node_observation.py`, `vpn_xui_node_executor.py`, `vpn_xray_runtime.py` | Reuse safe SQLite snapshots and trusted process/loopback runtime helpers; do not alter their mutation behavior |
| `backend/app/db/models.py`; new `backend/app/db/vpn_usage_migrations.py`; `backend/app/db/migrations.py` | Six small persistent tables and indexes |
| New `backend/app/services/vpn_usage.py`, `vpn_abuse.py` | Claim/read/apply and complete-window detection |
| `backend/app/services/control_runtime.py`, `backend/app/core/config.py` | One opt-in bounded background collector; shutdown cleanup |
| New `backend/app/api/routes/vpn_usage.py`, `backend/app/schemas/vpn_usage.py`; `backend/app/api/__init__.py` | Admin reports and incident acknowledgement |
| `frontend/src/api.ts`, `App.tsx`, `VpnCustomerWorkspacePanel.tsx`; new `VpnUsagePanel.tsx`, `VpnAbusePanel.tsx`, `vpnUsage.ts` | Node/customer periods and incident review in current overview/customer/events views |
| New tests `backend/tests/test_vpn_node_usage.py`, `test_vpn_usage.py`, `test_vpn_usage_postgres.py`, `test_vpn_abuse.py`, `test_vpn_usage_api.py`; new `frontend/test/vpnUsage.test.mjs` | Protocol, deltas, concurrency, privacy and UI behavior |

Current fleet health holds worker/endpoint locks during SSH. **Do not copy that lock duration.** Usage does not update endpoint readiness, control intents, the node control journal or `VpnAccessKey` credentials. CPU `WorkerNode.cpu_load` and `ram_usage_percent` already come from heartbeat and are labelled as load/RAM, not measured VPN bandwidth.

## Task 1: Separate read-only node usage protocol

- [ ] **RED:** Add protocol/observer tests to `test_vpn_node_usage.py`. Fixtures contain two managed clients, one unrelated client, duplicate email differing by case, a missing row, a WAL database, invalid integers/bools, excessive keys/sources/output, a changing Xray PID and an unsafe database symlink. Assert no SQL update/delete, panel mutation, stats reset, config write, journal write or service restart. Run from `backend`:

```powershell
& '..\..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest tests/test_vpn_node_usage.py -q
```

Expected initially: missing usage protocol import.

- [ ] **GREEN:** Define these concrete wire types in `vpn_node_usage.py` (JSON uses these snake_case names):

```python
@dataclass(frozen=True, slots=True)
class UsageIdentity:
    access_key_id: int
    inbound_id: int
    client_uuid: str
    client_email: str


@dataclass(frozen=True, slots=True)
class UsageRequest:
    version: int
    poll_id: str
    minute_ms: int
    identities: tuple[UsageIdentity, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class KeyUsage:
    access_key_id: int
    traffic_state: Literal["ok", "unavailable"]
    record_id: int | None
    reset_count: int | None
    upload_bytes: int | None
    download_bytes: int | None
    source_state: Literal["ok", "unsupported", "unavailable"]
    sources: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class UsageReceipt:
    version: int
    poll_id: str
    minute_ms: int
    observed_ms: int
    counter_epoch: str
    keys: tuple[KeyUsage, ...] = field(repr=False)
```

Use exact field sets, duplicate-JSON-key rejection, canonical UUID `poll_id`, version `1`, unique positive key/inbound IDs, canonical UUID credential strings and the existing safe email grammar. Integers must be `type(value) is int`, not booleans; counters/record IDs must fit signed int64, counters non-negative, record ID positive. Receipt key IDs exactly match requested IDs: missing/duplicate/extra rows fail the batch, not imply zero. Limit 2048 identities, 64 sources per key, 512 KiB request, 4 MiB receipt, 30-second clock skew, 15-second observation duration. Overflow is a generic coverage-unavailable result; never silently truncate. Add `shortcut: usage batches cap at 2048 managed credentials per node; add bounded pagination before raising this limit` beside the constant, and expose overflow as unavailable in reports.

Create `run_node_usage_entrypoint` using `_bounded_read`, root/config-file validation and secret-free exit/error conventions from `vpn_node_health_entrypoint.py`. Read one safe SQLite snapshot with `vpn_xui_node_observation._database_snapshot`; query managed emails in bounded parameter batches, selecting exactly `id,email,up,down,reset_count` from `client_traffics`. Confirm each email/UUID/inbound identity against the same node inventory; ambiguous or mismatched attribution is unavailable. Reuse existing verified email and UUID mappings; never derive identity from `public_name`/display name. Missing counters stay unavailable, with old totals retained by the backend.

Use the trusted Xray executable/config/process/start-tick/loopback checks in `vpn_xray_runtime`. Set `counter_epoch = sha256(boot_id + process_id + process_start_ticks).hexdigest()` and verify identity again after collecting; PID replacement invalidates the batch. Capture a stable SQLite row ID/reset_count in each key sample. No `-reset` or `-include-traffic` argument is ever used: persisted 3x-UI counters remain the traffic source.

Add bundle members, import-probe imports and `['--usage']` dispatch alongside existing `--health`; keep prior commands byte-compatible. Add `FIXED_NODE_USAGE_COMMAND = FIXED_NODE_COMMAND + " --usage"` and `execute_vpn_node_usage_over_ssh` in the existing transport module. Reuse `_connection_options`, pinned known-host validation, bounded pipe reads and cancellation cleanup; give only usage its larger stdout limit. No terminal/shell interpolation, unbounded `communicate`, retrying mutation or raw stdout/stderr logging. Parser failures clear exception context as existing strict boundaries do; raw IPs exist only in process memory and encrypted SSH bytes, never files/logs.

- [ ] Run GREEN: new node tests plus `test_vpn_node_bundle.py test_vpn_node_deployment.py test_vpn_node_transport.py test_vpn_node_health_entrypoint.py test_vpn_xui_node_observation.py test_vpn_xray_runtime.py`; subprocess-test the zipapp with `python -I -S` and fake files/process runner. Commit `feat(vpn): add bounded read-only node usage protocol`.

## Task 2: Persist conservative counter deltas and short collection claims

- [ ] **RED:** Add table/model and pure delta tests in `test_vpn_usage.py`. Cases: first sample, duplicate receipt, older sample, ordinary increase, either counter decreasing, reset_count/record/process change, missing sample, long outage, int64 overflow and concurrent apply. The core expected examples are:

```python
assert traffic_delta(None, sample(60, "a", 1, 0, 100, 300)) is None
assert traffic_delta(sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 0, 150, 390)) == (50, 90)
assert traffic_delta(sample(60, "a", 1, 0, 100, 300), sample(120, "b", 1, 0, 150, 390)) is None
assert traffic_delta(sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 1, 2, 4)) is None
```

Define the test helper as `sample(second, epoch, record, reset, up, down) -> SimpleNamespace(sampled_at=datetime.fromtimestamp(second, UTC), counter_epoch=epoch, record_id=record, reset_count=reset, upload_bytes=up, download_bytes=down)`; no historical traffic is invented by the baseline cases.

- [ ] **GREEN:** Add these tables to metadata and idempotent `VPN_USAGE_MIGRATIONS`, appended after device migrations:

| Table | Required columns and constraints |
| --- | --- |
| `vpn_node_usage_polls` | `worker_id` PK/FK RESTRICT; `claim_token` nullable UUID string; `lease_until`, `next_poll_at`, `last_attempt_at`, `last_success_at` nullable timestamps; `last_error_code` nullable safe code; `traffic_state`, `source_state` enum-like strings default `unavailable`; `protocol_version` nullable integer |
| `vpn_key_usage_samples` | `access_key_id` PK/FK RESTRICT, `worker_id` FK RESTRICT, `sampled_at`, `counter_epoch` varchar64, `record_id`, `reset_count`, `upload_bytes`, `download_bytes` BIGINT; non-negative checks; no raw email/UUID/IP |
| `vpn_usage_hours` | composite PK `(access_key_id, worker_id, hour_start)`; key/worker FKs RESTRICT; non-negative BIGINT `upload_bytes`, `download_bytes`; positive `sample_count`; `first_sample_at`, `last_sample_at`; index `hour_start` for 90-day cleanup |
| `vpn_key_source_samples` | `access_key_id` PK/FK RESTRICT; `worker_id`, `minute_at`, `observed_at`, `state`, `key_epoch` varchar16; JSON `source_hashes` (latest sample only); index `observed_at`; valid empty JSON list is distinct from unavailable |
| `vpn_device_abuse_state` | `device_id` PK/FK RESTRICT; `last_minute_at`, `started_at` nullable; `consecutive_windows` integer 0–5 default0; `last_source_count` nullable integer; `coverage` string default unavailable; `key_epoch` nullable varchar16 |
| `vpn_abuse_incidents` | integer PK; `device_id` FK RESTRICT; `started_at`, `last_observed_at`, `peak_source_count` >2; `acknowledged_at`, `acknowledged_by_user_id` nullable admin FK; partial unique open-incident index on device where acknowledged_at IS NULL; index `last_observed_at` |

Use existing handwritten migrations with guarded named constraints/indexes. SQLite metadata and PostgreSQL migrated schema must match; include both dialect-specific partial-index predicates for the open-incident index. No destructive backfill; existing credentials and traffic policy are untouched. Snapshot tables replace rows rather than accumulating every minute's source lists. Hourly rows are the only long-lived usage series.

The pure delta function is:

```python
def traffic_delta(previous, current) -> tuple[int, int] | None:
    if previous is None:
        return None
    if not 0 < (current.sampled_at - previous.sampled_at).total_seconds() <= 120:
        return None
    if (previous.counter_epoch, previous.record_id, previous.reset_count) != (
        current.counter_epoch, current.record_id, current.reset_count
    ):
        return None
    up = current.upload_bytes - previous.upload_bytes
    down = current.download_bytes - previous.download_bytes
    return (up, down) if up >= 0 and down >= 0 else None
```

Caller ignores duplicate/out-of-order samples before invoking this function and never moves the baseline backwards. A reset or gap >120 seconds saves a new baseline and marks that interval unmeasured. A normal delta belongs to the UTC hour of the ending sample; document `shortcut: minute deltas are booked to the ending UTC hour; split measured intervals if finer boundary attribution is required`. Reports identify partial coverage; a gap does not become measured zero or silently distribute bytes across unknown hours.

Implement `claim_usage_poll(session_factory, known_hosts_path, now)` as a short transaction: initialize missing poll rows safely, find due non-archived enabled VPN workers, lock worker then poll row with `skip_locked`, check active maintenance/control work and skip busy workers, snapshot verified key bindings and pinned transport, set unique claim token +45-second lease + next UTC minute, commit. At most one batch per node/minute; no dirty ORM state before policy/allocation helpers. Keys without verified identity get unavailable coverage, not a guessed email. Include all recorded child credentials still present, not just the selected country or a 1000-key admin list.

`collect_usage_poll` performs SSH **after claim commit**. `apply_usage_poll` opens a new short transaction, locks worker → poll row, verifies claim token/lease and recorded key bindings/fingerprint still match, then updates baselines and hourly sums atomically. No customer/subscription/key locks during network IO. A late/stale claimant cannot apply; duplicate apply is a no-op. Worker failure updates safe poll coverage/error only and never health/readiness, keys or last known measured totals. `protocol_version=1` is set only after a valid usage receipt; an old bundle failing `--usage` remains unsupported, never success with zeros.

**Verified lock correction:** PostgreSQL reproduced an implicit FK deadlock between first-baseline/new-hour inserts and control's key-before-worker locks. Before worker → poll, apply therefore acquires ordered nonblocking `FOR KEY SHARE` references for **every** captured key, rejecting incomplete coverage. These short reference locks occur only after SSH. Worker/poll acquisition is also nonblocking. Lease and receipt freshness are rechecked after all awaited reads using the entry UTC reference plus elapsed monotonic time, so pool waits cannot revive an expired claim.

- [ ] Run GREEN: unit tests, repeated migration/metadata-before-migrations startup tests in `test_vpn_usage_postgres.py`, and independent PostgreSQL sessions competing for one due node. Assert one claim, one aggregate increment and no issuance delay while the fake SSH reader is blocked on an event. Commit `feat(vpn): aggregate measured traffic without holding issuance locks`.

## Task 3: Schedule collection and show node/customer traffic

- [ ] **RED:** Extend `test_vpn_control_runtime.py` for disabled default, one due collector, four concurrent maximum, timeout cleanup, independent provisioning progress, shutdown and late completions. Add `test_vpn_usage_api.py` for admin authorization, periods 1/7/30, child aggregation, empty/unavailable/stale coverage and absence of credential/IP fields.
- [ ] **GREEN:** Add only `VPN_USAGE_ENABLED` (default false) to `Settings`; keep interval 60s, four SSH tasks, operation deadline 35s and retention 90 days as named module constants. In `ControlRuntimeOrchestrator`, schedule due usage polls outside its existing orchestration transaction, maintain a bounded task set, reap errors using fixed safe messages, cancel and await cleanup during shutdown. Add no second runtime/daemon or long transaction around a cycle. Reuse the dedicated bounded database session options already used by controller jobs if required for apply deadlines. Do not perform an implicit node upgrade or turn on Xray stats.

Add admin-only read routes with exact response fields:

```python
class UsageTotals(BaseModel):
    upload_bytes: int | None
    download_bytes: int | None
    measured_since: datetime | None
    last_sample_at: datetime | None
    coverage: Literal["none", "partial", "current", "stale", "unavailable"]


class NodeUsageRow(UsageTotals):
    worker_id: int
    active_devices: int
    occupied_profiles: int
    max_active_profiles: int | None
    cpu_load: float | None
    ram_usage_percent: float | None


class DeviceUsageRow(UsageTotals):
    device_id: int
    display_name: str


class CustomerUsageResponse(UsageTotals):
    customer_id: int
    days: Literal[1, 7, 30]
    devices: list[DeviceUsageRow]
```

`GET /control/vpn/usage/nodes?days=1` returns node rows (paginated by worker ID, limit100); `GET /control/vpn/customers/{customer_id}/usage?days=1` returns customer totals and their device rows. Join hourly rows through all child keys/subscriptions/devices for customer totals; never multiply rows through an ungrouped join. Node totals use the hour's recorded worker ID. The first baseline yields `None` totals until a measured delta interval exists; a genuine measured interval with zero delta yields zero. Keep last measured totals visible with stale/partial labels. For nodes with multiple endpoints sum endpoint capacity reservations, and count distinct active logical device IDs; describe it as active logical profiles, never physical devices or measured bandwidth. Heartbeat age gates CPU/RAM visibility; stale heartbeat yields null rather than its model default zero.

Use a literal allowlist for days and UTC boundaries; avoid arbitrary group-by SQL. Clean `vpn_usage_hours` older than 90 days in bounded 1000-row batches; source samples older than24h are removed even if a node stays offline. Do not delete the last traffic baseline during ordinary cleanup. Cleanup uses the same runtime, at most one bounded batch/minute and no network.

Add `VpnUsagePanel` to the existing nodes/overview views and per-customer `VpnCustomerWorkspacePanel` area. Reuse current controls for 1/7/30 days, a simple table and `displayMetric`; no chart dependency. Show `Нет данных`, `Частичные данные`, `Последнее измерение …`, `Занято профилей / лимит`, `Нагрузка CPU`, `RAM`. Preserve separate traffic/health/capacity meanings. Request epochs discard a previous customer's late response.

- [ ] Run GREEN: `test_vpn_usage.py test_vpn_usage_api.py test_vpn_control_runtime.py`, PostgreSQL usage tests, `npm test` and `npm run build` from `frontend`. Unit-test `formatTrafficBytes(null) == "Нет данных"`, actual zero formatting and child-key sums; browser-check no-data/stale values on desktop/mobile. Commit `feat(vpn): display measured usage with coverage status`.

## Task 4: Probe real online-source support and store only HMAC samples

- [ ] **RED:** Add node fixtures for StatsService absent, loopback API exposed publicly, disabled `statsUserOnline`, unsupported CLI, empty successful output with disabled policy, policy-enabled empty inventory, malformed source, IPv4-mapped IPv6, same IPv6 /64, process/config drift and oversized output. Unsupported/unavailable never equals zero.
- [ ] **GREEN:** Extend the separate usage observer, not health/controller receipts. Inspect the actual trusted running Xray binary and config; require `stats` enabled, API services including `StatsService`, verified loopback-only listener owned by that process, effective `policy.levels[level].statsUserOnline == true` for the managed runtime users, and successful bounded `api statsonlineiplist --server=127.0.0.1:<port> --timeout=3 -all`. Resolve actual user levels using the existing bounded `api inbounduser -tag=<runtime tag>` inventory, not an assumed default level if runtime differs. Do not add `-reset`, access logs, public API binding or write config. Any missing prerequisite reports unsupported/unavailable; the collector never enables it or restarts Xray.

The CLI's `users` array contains email and `ips` entries; reject duplicates and attribute only to the verified managed identity. Prove effective policy before interpreting absence from a successful complete all-users response as no active sources. Merely recognizing the command or receiving an empty array is insufficient. These mechanics are verified in the pinned [Xray 26.9.9 CLI](https://github.com/XTLS/Xray-core/blob/v26.9.9/main/commands/all/api/stats_online_ip_list.go), [stats protobuf](https://github.com/XTLS/Xray-core/blob/v26.9.9/app/stats/command/command.proto), and [policy config](https://github.com/XTLS/Xray-core/blob/v26.9.9/infra/conf/policy.go). The supported 26.7.28 version has the same all-users CLI form; still test/probe the installed binary.

Normalize and hash immediately in backend memory before any database call:

```python
def source_hash(secret: str, device_id: int, raw: str) -> str:
    if "%" in raw:
        raise ValueError("vpn_usage_source_invalid")
    value = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    address = ipaddress.ip_address(value)
    if value != raw and not isinstance(address, ipaddress.IPv6Address):
        raise ValueError("vpn_usage_source_invalid")
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv6Address):
        normalized = f"6:{ipaddress.ip_network((address, 64), strict=False)}"
    else:
        normalized = f"4:{address}"
    message = f"vpn-abuse-network-source-v1\0{device_id}\0{normalized}".encode("ascii")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
```

Require a persistent non-default session secret of at least32 characters; if invalid, traffic may still work but source coverage is unavailable. Scope pseudonyms by device, allowing cross-node deduplication for that device without creating cross-customer IP tracking. Store only unique hashes plus HMAC key epoch (domain-separated digest prefix), counts/coverage and timestamps. Key rotation resets streaks. Accept the bracketed IPv6 form used by [Xray's address formatter](https://github.com/XTLS/Xray-core/blob/v26.9.9/common/net/address.go), but reject invalid IP/zone IDs/ports; never log them or transmit the backend secret to nodes. IPv4 plus IPv6 can represent one physical device; all UI labels say network sources. Changing IP with only one or two current sources never triggers an incident. NAT can hide sharing and is not counted as one physical device.

Add privacy tests exercising request-parser exceptions, transport stdout errors, dataclass repr and DB parameters/events: no raw IP, URI, account email or UUID in persisted source data or log records. Traffic rows retain only internal key IDs. Do not serialize raw IPs into debug fixtures/artifacts from a live node.

- [ ] Run GREEN: node usage/privacy tests, `test_vpn_node_bundle.py test_vpn_node_transport.py test_vpn_xray_runtime.py`, usage application tests. Commit `feat(vpn): collect capability-checked private source evidence`.

## Task 5: Complete-window detection and administrator review

- [ ] **RED:** In `test_vpn_abuse.py`, use five minute timestamps and actual source samples. Cover >2 for four windows (none), fifth complete window (one incident), duplicate fifth window (still one), a missing minute (reset), unsupported/missing child (reset), cross-node same source (dedupe), observation skew >15s (unknown/reset), source key epoch change (reset), acknowledgement followed by five fresh windows (new incident), revoked device and late old snapshot. Use PostgreSQL concurrent evaluators/acknowledgements in `test_vpn_usage_postgres.py`.
- [ ] **GREEN:** Evaluate only when every non-revoked bound child of a logical device has a current successful source sample in the **same UTC minute**, effective identity/policy coverage, identical HMAC key epoch and at most15 seconds between observations. Include non-selected countries and pending-revoke/suspended/uncertain reservations; unsupported or unbound still-reserved children make device coverage incomplete. Do not add source counts from different minute windows. Missing samples may remain collecting until minute+90seconds; expiry resets evidence, as does an explicit unavailable result. A new child created mid-window cannot be omitted to manufacture complete coverage.

Lock only the device's `vpn_device_abuse_state` row for a short apply transaction. This lock is independent of credential mutation and is never held during network IO. `last_minute_at` prevents duplicate/out-of-order advancement. For a complete new minute with `count >2`, increment by one only if previous completed minute is exactly60seconds earlier and epoch unchanged, otherwise start at1; cap at5. `count <=2`, missing/unavailable coverage or a gap resets to0. At5 insert one open incident or update its last time/peak; a partial unique index and the state lock prevent duplicates.

Add admin-only `GET /control/vpn/abuse/incidents?state=open|acknowledged&after_id=0&limit=100`, returning `id,device_id,customer_id,display_name,started_at,last_observed_at,peak_source_count,acknowledged_at`. Add `POST /control/vpn/abuse/incidents/{id}/acknowledge` requiring admin + same-origin. Lock abuse-state → incident consistently; set ack actor/time, clear streak and audit IDs only. Ack does not revoke access or alter key policy. Keep incidents after short-lived source samples expire, without source hash lists in the incident response.

Mount `VpnAbusePanel` in existing `#vpn/events`: show `Подозрение на совместное использование`, `Сетевых источников: …`, interval, coverage and `Это сигнал для проверки, а не число физических устройств`. Provide acknowledge and open-customer/device actions. Reuse the existing device-aware revoke and issue-profile flows: explicit revoke stages every child and invalidates subscription URL; replacement uses a fresh device only when the existing slot policy allows it. Do not create an abuse-specific revoke transport, bypass capacity, silently revive a revoked device or promise existing tunnels are instantly terminated. Where acknowledgement is followed by continuing excess, five new complete windows may create a new visible incident.

- [ ] Run GREEN: all new backend tests, `test_vpn_devices.py test_vpn_policy.py test_vpn_control_intents.py test_vpn_lifecycle.py`, isolated PostgreSQL concurrency/migration tests, `npm test`, `npm run build` and a browser walkthrough with fake telemetry. Assert the full abuse pipeline makes **zero** revoke/suspend/restart/IP-ban calls. Commit `feat(vpn): surface sustained source-sharing incidents for review`.

## Verification and operational gates

- [ ] Root supplies the disposable `VPN_PORTAL_TEST_PG_URL` runner; use the existing `postgres_schema` safe URL validation, never application `DB_URL`. PostgreSQL races/migration tests must execute rather than skip. Do not print credential-bearing URLs.
- [ ] Run `git diff --check`; review only these slice files and preserve unrelated dirty work. Before success, report exact tests and skips, including node-bundle dependency-free import probe.
- [ ] Node rollout uses the existing digest-verified bundle deployment and known-host flow after backup/restore gates. Verify version/capability before collection; no hidden StatsService/config enabling or shared restart is authorized.

Known limits: traffic before the first valid baseline and across unmeasured gaps cannot be reconstructed; minute deltas have ending-hour attribution. Source evidence is a compatible heuristic with explicit coverage, not physical-device enforcement. Real production node capability and actual traffic accuracy require a separate authorized smoke check; none is performed by writing or locally testing this plan.
