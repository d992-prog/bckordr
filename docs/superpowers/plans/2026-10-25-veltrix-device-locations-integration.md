# Veltrix Publication and Device Locations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let administrators publish locations and let an authenticated owner select a location inside one logical device, imported through a permanent revocable subscription URL.

**Architecture:** Build on the device foundation plan. Publication filters new allocations independently from node readiness; selecting a new endpoint lazily creates/reuses a child credential and changes `preferred_key_id` only after a verified successful provision. Add `/vpn-portal/v2/devices` APIs and a secret-bearing subscription feed, preserving all existing `/profiles/{key_id}` routes and stored direct links.

**Tech Stack:** Existing FastAPI/SQLAlchemy controller queue, `itsdangerous` and Python standard library, React/Vite, existing Node tests/browser QA, isolated PostgreSQL concurrency tests. No external subscription service or new dependency.

**Implementation checkpoint:** Publication, device-location services, v2 APIs, signed subscription feeds and both admin/customer interfaces completed; specification and quality reviews passed. PostgreSQL ownership/allocation/revoke races executed, and mounted browser checks passed. Deployment, intentional publication of production endpoints and real Happ/Hiddify refresh remain release gates.

---

Dependency: `2026-10-25-veltrix-device-locations-foundation.md` complete and verified. Implement Tasks 1–2 as a bounded publication slice, Tasks 3–5 as the backend location/subscription slice, and Task 6 as the frontend integration slice. Review and commit each slice before dispatching the next. No production migration/deploy, payment or real Telegram messages in these tasks.

## File map and concrete contracts

| File | Responsibility |
| --- | --- |
| `backend/app/db/models.py`, `backend/app/db/vpn_device_migrations.py` | Add publication fields and restart-safe migration |
| `backend/app/services/vpn_policy.py` | Reuse existing health/capacity selection with publication and exact endpoint filters |
| `backend/app/api/routes/control.py`, `backend/app/schemas/control.py` | Audited publication endpoint and capacity response metadata |
| `backend/app/services/vpn_devices.py` | Canonical locked owner context, location preparation and success promotion |
| `backend/app/services/vpn_device_view.py` (new) | Safe logical-device/location metadata and owned ready-connection lookup |
| `backend/app/services/vpn_device_subscriptions.py` (new) | Signed tokens and one-selected-URI subscription encoding |
| `backend/app/api/routes/vpn_portal.py`, `backend/app/schemas/vpn_portal.py` | Additive v2 endpoints and public token feed |
| `backend/app/services/vpn_control_intents.py` | Promote pending selection only in successful observed finalization |
| `backend/app/services/vpn_ready_notifications.py` | Notify once for initial device creation, not for each country credential |
| `backend/app/services/vpn_portal_logging.py`, `deploy/nginx-vpn-portal-http.conf` | Redact the entire subscription token in all access/application logging |
| `frontend/src/api.ts`, `VpnEndpointCapacityPanel.tsx`, `vpnCustomerWorkspace.ts`, `VpnCustomerWorkspacePanel.tsx` | Publication controls and authoritative subscription usage |
| `frontend/src/vpn-portal/types.ts`, `api.ts`, `connection.ts`, `Portal.tsx`, `PortalHome.tsx`, `ProfileCard.tsx`, `portal.css`, new `DeviceLocationPicker.tsx` | One card per logical device; location selection and subscription import |
| New backend tests `test_vpn_device_locations.py`, `test_vpn_device_locations_postgres.py`, `test_vpn_device_subscriptions.py`; existing portal/public issuance/logging/nginx tests | Security, consistency, compatibility and concurrency |
| New frontend `test/vpnDeviceLocations.test.mjs`; existing API/connection/browser QA | Interaction, stale responses and session isolation |

Publication fields are `is_published: bool = False`, `public_name: str | None` (64 characters), `country_code: str | None` (two ASCII uppercase letters). Hidden nodes never fall back to a technical server name. A current hidden legacy endpoint is shown as current, with its public metadata if available, otherwise `Ранее выданное подключение`; it is not a selectable option.

The owner API contracts are:

```python
class PortalDevice(PortalProfile):
    legacy_profile_id: int | None


class PortalDeviceLocation(BaseModel):
    endpoint_id: int
    name: str
    country_code: str | None
    is_published: bool


class PortalDeviceLocations(BaseModel):
    current: PortalDeviceLocation | None
    pending_endpoint_id: int | None
    selection_state: Literal["ready", "preparing", "failed", "verification_required", "unavailable"]
    available: list[PortalDeviceLocation]


class SelectPortalDeviceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: int = Field(gt=0)


class PortalDeviceConnection(BaseModel):
    subscription_url: str
    direct_uri: str
```

`PortalDevice.id` is the logical device ID. It is never used as a credential ID. `legacy_profile_id` explicitly retains the old direct-profile mapping; legacy `/profiles/{id}/connection` and rename continue accepting **key IDs**, including children, and preserve ownership checks. The new frontend uses the v2 device routes throughout.

## Task 1: Publication schema and administration

- [ ] **Step 1: Write failing tests** in `backend/tests/test_vpn_device_locations.py`: a newly created endpoint is hidden; publishing requires valid public name and country; unknown/foreign fields return 422; unauthenticated/non-admin returns 401/403; cross-origin publication returns 403; publication and hiding do not modify any key or queue any operation; publishing a temporarily unhealthy node is allowed but does not make it eligible. Reuse the ASGI admin harness from `test_vpn_control_api.py`.
- [ ] **Step 2: Run RED** from `backend` with the foundation plan's Python executable and `-m pytest tests/test_vpn_device_locations.py -q`.
- [ ] **Step 3: Add fields to `VpnEndpoint`**:

```python
is_published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
public_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
```

Append these statements to the device migration tuple (or a separately named publication tuple appended after it), keeping fresh metadata and upgraded schema equivalent:

```sql
ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS public_name VARCHAR(64) NULL;
ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS country_code VARCHAR(2) NULL;
```

Add `VpnEndpointPublicationUpdateRequest` with `ConfigDict(extra="forbid")`, required `is_published: bool`, `public_name: str`, `country_code: str`. Apply existing `validate_display_name` to the public name and `Field(pattern=r"^[A-Z]{2}$")` to the stripped/uppercased country. Add the three response fields to `VpnEndpointCapacityResponse` and `_vpn_endpoint_capacity_response`.

Add `PATCH /control/vpn/endpoints/{endpoint_id}/publication`: `require_admin`, `_require_same_origin(request)`, discover worker ID without mutation, lock worker then endpoint with `populate_existing=True`, verify endpoint still belongs to that worker, assign only the three publication fields and updated timestamp. Audit `action="vpn_endpoint_publication_update"` and `details=f"endpoint_id={endpoint.id} published={int(payload.is_published)}"`; never include credentials. Return the existing capacity response with actual occupancy. No remote IO or readiness mutation.

- [ ] **Step 4: Run GREEN** with `test_vpn_device_locations.py test_vpn_control_api.py test_vpn_endpoint_migrations.py test_vpn_device_migrations_postgres.py`; add repeated-upgrade coverage asserting existing endpoints stay hidden and credentials unchanged.
- [ ] **Step 5: Commit**: `feat(vpn): add explicit endpoint publication`.

## Task 2: Apply publication once in shared allocation policy

- [ ] **Step 1: Add failing policy tests** in `test_vpn_public_trial.py` using its existing `worker`, `endpoint` and `add_key` helpers:

```python
async def test_hidden_endpoint_cannot_receive_public_allocation(session_factory):
    async with session_factory() as db:
        node = worker("hidden")
        db.add(node)
        await db.flush()
        target = endpoint(node, is_published=False)
        db.add(target)
        await db.flush()
        selected = await select_public_vpn_endpoint(
            db, now=NOW, health_max_age_seconds=120, lock=True)
        assert selected is None
        assert await select_public_vpn_endpoint(
            db, now=NOW, health_max_age_seconds=120,
            worker_id=node.id, require_published=False, lock=True) is not None
```

Also assert a published full endpoint is unavailable to a new child but selectable when reusing that device's already reserved credential. Pending/failed/suspended children already consume node capacity and must not require another place for retry/resync; only a ready child can be promoted immediately, while the others still require the normal controller checks. Revoked/unowned credentials supply no capacity credit. Publication never bypasses stale health, insecure transport, missing verification, maintenance or active-attack exclusions.

- [ ] **Step 2: Run RED**: `-m pytest tests/test_vpn_public_trial.py -q`.
- [ ] **Step 3: Extend `select_public_vpn_endpoint`** with these keyword arguments:

```python
endpoint_id: int | None = None,
require_published: bool = True,
reusable_key_id: int | None = None,
```

Filter `endpoint_query` by exact `endpoint_id` if provided. At the start of its inner `capacity` function, reject if `require_published and not endpoint.is_published`. Keep every current readiness/health/worker check. Replace only the final full-capacity rejection with:

```python
reusable = False
if reusable_key_id is not None:
    reusable = await session.scalar(select(VpnAccessKey.id).where(
        VpnAccessKey.id == reusable_key_id,
        VpnAccessKey.endpoint_id == endpoint.id,
        VpnAccessKey.status.in_(NODE_CAPACITY_STATUSES),
        VpnAccessKey.revoked_at.is_(None),
        VpnAccessKey.revoke_requested_at.is_(None),
    )) is not None
if occupied >= limit and not reusable:
    return None
```

The location service supplies `reusable_key_id` only after same-device and same-subscription ownership checks. The `lock=True` path must re-read publication, health and capacity under existing worker → endpoint locks. Do not add dirty ORM objects before calling it.

Public trial and invitation redemption use the default publication requirement. In `create_vpn_access_key`, use `require_published=payload.worker_id is None`; explicit admin worker choice may use a hidden endpoint. If automatic VLESS selection finds no published endpoint, return 409 `No ready VPN endpoint is currently available` **before** `_resolve_vpn_key_worker`; otherwise its legacy fallback could bypass publication. Keep existing explicit legacy worker issuance and VMess handling intact.

Update public-success fixtures to `is_published=True` in `test_vpn_public_trial.py`, `test_vpn_public_trial_postgres.py`, `test_vpn_friend_invitations.py`, `test_vpn_friend_invitations_postgres.py`, `test_vpn_friend_invitation_api.py` and public auto-issuance cases in `test_vpn_control_api.py`. Do not change the model default or make hidden fixtures implicitly eligible.

- [ ] **Step 4: Run GREEN**: the six files above plus `test_vpn_policy.py`. Confirm manual hidden issuance works, automatic hidden-only issuance fails without creating a device/key, and existing hidden keys remain readable/resyncable.
- [ ] **Step 5: Commit**: `fix(vpn): require publication for public endpoint allocation`.

**Publication race decision already accepted:** hiding blocks subsequent user allocations and selections. A child whose reservation transaction committed before hiding may finish provisioning; existing ready connections remain usable. Do not add a public-issuance flag or cancel previously accepted jobs. A fresh retry requiring a new allocation must recheck publication; ordinary resync of a recorded credential preserves the existing endpoint.

## Task 3: Atomic location selection and late-result protection

- [ ] **Step 1: Add failing service tests** in `test_vpn_device_locations.py` for this sequence: A active/current → select B → A still downloadable → B observed success → B current → select A → same A UUID reused and no extra slot/key. A failed/uncertain B never replaces A. Add A-pending → B-pending → late-A-success case, asserting `pending_key_id == B.id` and no promotion of A. Reject foreign device, revoked device, inactive customer/subscription, hidden/unhealthy/full endpoint and unsupported target, leaving the transaction unchanged.
- [ ] **Step 2: Run RED** on `test_vpn_device_locations.py`.
- [ ] **Step 3: Implement these service entry points** in `vpn_devices.py`:

```python
async def select_device_location(
    db: AsyncSession, *, customer_id: int, device_id: int,
    endpoint_id: int, settings: Settings, now: datetime,
) -> VpnDevice:
```

Acquire/refresh customer → all their subscriptions in ID order → all their access keys in ID order, using the existing control-intent queries. Read/refresh the owned device after those locks, ensuring it still belongs to one of the locked subscriptions. No separate device row lock is needed because every device writer and finalizer holds the same customer lock. Reject a device with `revoke_requested_at`; validate subscription dates/status/customer with `enforce_device_limit=False` because the device already exists. Never attach a newly requested location as an independent device.

Find the child by `(device_id, subscription_id, endpoint_id)`. Reject revoked/pending-revoke child; it cannot be silently re-created because the uniqueness constraint deliberately retains its history. Call shared endpoint selection with `endpoint_id`, default publication and `reusable_key_id=child.id` only for that owned, non-revoking child; this credits its existing reservation, without claiming it is ready. Check `load_transport_snapshot(worker, Path(settings.vpn_control_known_hosts_path))` as public trial does. Only **after** allocation locks succeed may new ORM state be added.

For an already ready/readable child, set preferred immediately, clear pending, update timestamp and flush. Otherwise create exactly this credential if absent:

```python
child = VpnAccessKey(
    device_id=device.id, subscription_id=subscription.id,
    worker_id=endpoint.worker_id, endpoint_id=endpoint.id,
    protocol="vless", display_name=device.display_name,
    external_uuid=str(uuid4()), panel_sub_id=uuid4().hex,
    status="pending_sync", issued_at=now, expires_at=subscription.expires_at,
)
db.add(child)
await db.flush()
child.verified_client_email = build_vpn_client_email(child.id, None)
await db.flush()
```

Set `device.pending_key_id = child.id`, update timestamp, flush, then call `stage_vpn_control_operation(db, child.id, "provision", now=now, retry_failed=True)`. Existing queued/claimed operation replay is idempotent; existing uncertain operation returns a conflict and requires reconciliation. A failure rolls back a new child, pending change and operation together. Never stage suspend/revoke for country selection.

Add this finalization helper in the same module:

```python
async def promote_prepared_device_key(db: AsyncSession, key: VpnAccessKey, *, now: datetime) -> None:
    if key.device_id is None:
        return
    device = await db.scalar(select(VpnDevice).where(
        VpnDevice.id == key.device_id,
        VpnDevice.subscription_id == key.subscription_id,
    ).execution_options(populate_existing=True))
    if (device is None or device.revoke_requested_at is not None
            or device.pending_key_id != key.id or key.status != "active"
            or key.revoke_requested_at is not None or key.revoked_at is not None
            or not key.config_uri):
        return
    device.preferred_key_id = key.id
    device.pending_key_id = None
    device.updated_at = now
```

In `finalize_vpn_control_operation`, invoke it only in the successful `observed` provision branch, **after** assigning `active` and `config_uri`, before final flush. Use a local import to avoid the `vpn_devices` → intent service cycle. The helper must not refresh/discard dirty key state. Failed, uncertain, blocked, superseded or stale receipts do not invoke promotion. Existing reconciliation calls this same finalizer, preserving atomicity. Do not hook network IO or commit into the helper.

Restrict `claim_ready_notice` to initial device credentials: a linked key is eligible only when its device's `legacy_profile_id` equals the key ID. Retain unlinked legacy-key compatibility. Add tests showing initial creation sends one notice, a second country sends none, and existing retry/claim behavior is unchanged. Do not set a fake `ready_notified_at` to suppress country notices. Otherwise every location switch would generate an unrelated profile-ready message.

- [ ] **Step 4: Run GREEN**: location service tests plus `test_vpn_control_intents.py test_vpn_control_dispatcher.py test_vpn_control_reconciliation.py test_vpn_lifecycle.py`.
- [ ] **Step 5: Add PostgreSQL barriers** in `test_vpn_device_locations_postgres.py`: concurrent same-endpoint selections produce one child and one current intent; different-endpoint selections obey last committed pending choice; selection/revoke race cannot produce a usable child after sticky revoke; two customers competing for one capacity slot admit one child. Run with `postgres_schema`, no skipped cases, then commit `feat(vpn): prepare device locations through scoped controller`.

## Task 4: Secret subscription URLs, logging first

- [ ] **Step 1: Before adding an exposed route, add failing log tests** to `test_vpn_portal_logging.py` and `test_vpn_portal_nginx.py`. Test raw path, full URL, query suffix, invalid token, Uvicorn five-argument access record, ordinary logger message and exception traceback containing `https://vpn.example/api/vpn-portal/subscription/SECRET`. All rendered records must omit `SECRET`; token paths in Nginx map must become `/api/vpn-portal/subscription/redacted`. Retain the existing server-level `/dev/null` error sink for pre-location failures.
- [ ] **Step 2: Run RED**, then add a token-path regex to `_sanitize_text` before other substitutions:

```python
_SUBSCRIPTION_TOKEN_PATH = re.compile(r"(/vpn-portal/subscription/)[^/\s?\"'<>]+")
value = _SUBSCRIPTION_TOKEN_PATH.sub(r"\1redacted", value)
```

Also make `_safe_access_target` return the redacted path with empty query/fragment for this prefix. Add a rule above `default $uri` in `map $uri $veltrix_safe_uri`:

```nginx
~^/api/vpn-portal/subscription/ /api/vpn-portal/subscription/redacted;
```

Application redaction must not depend on the configured API prefix: test both `/api/vpn-portal/subscription/SECRET` and `/custom/vpn-portal/subscription/SECRET`, including Uvicorn access arguments. Build its redacted target from the matched path and drop the entire query/fragment. The checked-in Nginx deployment uses `/api`; deployments changing that prefix must update the matching Nginx rule too.

No distinct Nginx location is needed: existing portal API location already applies rate limiting, no-store/no-referrer headers and a server error sink. Add `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff` to application token responses too. Existing portal middleware supplies `Cache-Control: no-store` even on errors. Add the subscription service logger prefix to safe logging registration if it logs at all; the new service should have no payload logging.

- [ ] **Step 3: Run GREEN** logging/Nginx tests and commit `fix(vpn): redact device subscription token paths` **before enabling the token route**.

- [ ] **Step 4: Write failing signer/feed tests** in `test_vpn_device_subscriptions.py`: deterministic URL across location/name changes, tamper, wrong salt, generation rotation, malformed payload types including booleans, foreign preferred key, expiry, customer disabled, manual revoke, invalid URI; body decodes to exactly one selected ready connection plus newline. Suspended/uncertain pending B leaves usable A in feed. A current hidden endpoint is still usable. No response contains token or URI in an error.
- [ ] **Step 5: Implement the small signer** in new `vpn_device_subscriptions.py`:

```python
import base64
import hashlib
from itsdangerous import BadData, URLSafeSerializer

SALT = "veltrix-vpn-device-subscription-v1"


def device_signer(settings: Settings) -> URLSafeSerializer:
    secret = settings.session_secret_key
    if len(secret) < 32 or secret == "change-me-session-secret":
        raise ValueError("device_subscription_unavailable")
    return URLSafeSerializer(secret, salt=SALT, signer_kwargs={"digest_method": hashlib.sha256})


def device_token(settings: Settings, device: VpnDevice) -> str:
    return device_signer(settings).dumps([device.id, device.token_generation])


def decode_device_token(settings: Settings, token: str) -> tuple[int, int]:
    if not isinstance(token, str) or not 1 <= len(token) <= 512:
        raise ValueError("device_subscription_unavailable")
    try:
        payload = device_signer(settings).loads(token)
    except BadData:
        raise ValueError("device_subscription_unavailable") from None
    if (type(payload) is not list or len(payload) != 2
            or any(type(value) is not int for value in payload)
            or payload[0] <= 0 or payload[1] < 0):
        raise ValueError("device_subscription_unavailable")
    return payload[0], payload[1]


def encode_device_subscription(uri: str) -> str:
    return base64.b64encode((uri + "\n").encode("utf-8")).decode("ascii")
```

`Settings`/`VpnDevice` imports come from their existing modules. Use `public_origin(settings)` plus configured API prefix to build the HTTPS URL, never request Host/forwarded headers. No token expiry is added: subscription/device/key validity is checked on every download; rotation increments generation. Reuse the already persistent session secret with domain-separated salt; do not add or log another secret. A weak/default secret fails closed for URL issuance and download. Changing the persistent server secret invalidates every device URL and is an explicit operational rotation.

Resolve device by signed ID and exact generation, join its current subscription/customer, reject device manual revoke, and load preferred key by **all three** `(preferred_key_id, device_id, subscription_id)`. Call existing `may_read_connection` and `display_uri` with device display name. Refresh all ORM reads with `populate_existing=True`; do not rely on auth-time snapshots. Return only the preferred key, not every prepared child. Public feed requires the token, not a browser session; it performs no redirects or external requests.

- [ ] **Step 6: Run GREEN** signer/feed tests; commit `feat(vpn): serve revocable selected-location subscriptions` together with the route task below, not as an exposed untested partial API.

## Task 5: Add owned v2 routes without changing key-ID routes

- [ ] **Step 1: Add failing ASGI tests** to `test_vpn_portal_api.py`: same-customer devices list once despite multiple children; own initial key ID and logical ID deliberately differ; foreign key/device returns generic 404; no URI/token in list/locations; mutation needs CSRF + exact origin and revalidated `lock_current_principal`; expired/revoked session cannot choose a location even if captured before waiting for customer lock.
- [ ] **Step 2: Add the contract models above**, then routes in existing `vpn_portal.py`:

| Method and path | Authorization | Success |
| --- | --- | --- |
| `GET /v2/devices` | `current_customer` | `list[PortalDevice]`, one per device |
| `PATCH /v2/devices/{device_id}` | `require_mutation`, revalidated principal under lock | `PortalDevice`, device/all-child rename |
| `GET /v2/devices/{device_id}/locations` | `current_customer` | `PortalDeviceLocations` |
| `POST /v2/devices/{device_id}/location` | `require_mutation`, revalidated principal under lock | 202 + `PortalDeviceLocations`; commit after staging |
| `GET /v2/devices/{device_id}/connection` | `current_customer` | `PortalDeviceConnection`, both secrets only on demand |
| `GET /subscription/{token}` | valid signed token + live policy checks | plain-text base64 of one URI |

Reuse `lock_current_principal` as trial activation does before write transactions; it obtains the customer lock before other policy rows. On missing owner return 404 `customer_device_not_found`; policy/selection conflicts return 409 `customer_location_unavailable`; uncertain operation returns 409 `customer_location_verification_required`. Public feed returns the same generic 404 for malformed, expired, revoked and unavailable tokens. Never echo exception strings, payloads or request URL.

The download response is:

```python
return Response(
    encode_device_subscription(uri), media_type="text/plain",
    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
             "X-Content-Type-Options": "nosniff"},
)
```

In `vpn_device_view.py`, derive `PortalDevice.state` from preferred child plus manual latch; `can_connect` uses the owned current preferred key and live policy. If device revoked, show `pending_revoke` while any child reserves a slot, otherwise `revoked`. Current active A remains active/connectable while B prepares. Location state: no pending → ready if current readable else unavailable; pending failed operation → failed; pending uncertain operation → verification_required; otherwise preparing. A stale/superseded operation cannot claim readiness. List only published eligible locations, with reusable current-device children allowed at capacity. Current hidden/unhealthy endpoint metadata is returned separately; it is never inserted into `available`.

GET metadata performs no mutation or remote probe. Existing root device migration ensures all production keys are linked; no lazy database writes on GET. Unlinked SQLite fixture keys continue to work through old APIs but do not appear as invented v2 devices.

- [ ] **Step 3: Run GREEN**: `test_vpn_portal_api.py test_vpn_customer_view.py test_vpn_device_locations.py test_vpn_device_subscriptions.py test_vpn_portal_logging.py test_vpn_portal_nginx.py`, then isolated PostgreSQL selection/revoke/capacity races. Confirm old direct routes still operate on key IDs and existing stored URLs are unchanged.
- [ ] **Step 4: Commit** the owned APIs and token serving with `feat(vpn): expose device location and subscription APIs`.

## Task 6: Integrate the two existing frontends

- [ ] **Step 1: Add failing API tests** to `frontend/test/vpnPortalApi.test.mjs`, `vpnCustomerApi.test.mjs`, `vpnEndpointCapacity.test.mjs`: new namespace/methods/bodies, same-origin credentials, no-store, CSRF only on mutations. Adjust the exact exported-method list assertion intentionally. Add the actual client entries:

```typescript
devices: () => portalRequest<PortalDevice[]>("/v2/devices"),
deviceLocations: (id: number) => portalRequest<PortalDeviceLocations>(`/v2/devices/${id}/locations`),
selectDeviceLocation: (id: number, endpointId: number, csrf: string) =>
  portalRequest<PortalDeviceLocations>(`/v2/devices/${id}/location`, {
    method: "POST", headers: { "X-CSRF-Token": csrf },
    body: JSON.stringify({ endpoint_id: endpointId }),
  }),
deviceConnection: (id: number) => portalRequest<PortalDeviceConnection>(`/v2/devices/${id}/connection`),
renameDevice: (id: number, displayName: string, csrf: string) =>
  portalRequest<PortalDevice>(`/v2/devices/${id}`, {
    method: "PATCH", headers: { "X-CSRF-Token": csrf },
    body: JSON.stringify({ display_name: displayName }),
  }),
```

Keep old `profiles`, `connection` and `rename` methods for existing callers/tests, but switch active portal loading to `devices`, card rename to `renameDevice`, and connection preparation in **both** `PortalHome.tsx` and `ProfileCard.tsx` to `deviceConnection`. In `connection.ts`, change `loadConnectionUri` to retrieve `deviceConnection(id).subscription_url` and keep its existing empty-value rejection/copy behavior. Type portal state/card props as `PortalDevice`; presentation helpers accepting the structural `PortalProfile` subset can remain unchanged. Add TypeScript versions of the backend contract models without secrets on the list type.

- [ ] **Step 2: Run RED** from `frontend`: `npm test`.
- [ ] **Step 3: Add `DeviceLocationPicker.tsx`** inside each profile card, reusing existing card/forms/buttons. Props: `deviceId`, `csrfToken`, `sessionGeneration`, `onUnauthorized`, `disabled`. Load locations on mount and on explicit refresh; native labelled `<select>` shows `country_code · name`, preserves current text separately and offers no hidden option. Selecting submits only on explicit button click. Use a request epoch ref and `SessionGeneration` for every async result; increment the epoch before each selection and on unmount/session invalidation. Do not let a pending earlier GET replace a newer POST's view. Disable inputs while a request is in flight and while renaming; provisioning alone does not prevent choosing another location.

On `preparing`, GET locations every 2 seconds for at most 30 attempts, then show a manual `Обновить статус` button. Stop polling on failed/verification_required, unmount or logout; do not retry a provisioning POST automatically. Show these exact messages beside the selector:

```typescript
const locationMessages = {
  ready: "После смены сервера обновите подписку в Happ или Hiddify и переподключитесь.",
  preparing: "Готовим подключение. Текущий сервер остаётся доступен.",
  failed: "Не удалось подготовить сервер. Выберите его снова, чтобы повторить попытку.",
  verification_required: "Результат подготовки проверяется. Можно выбрать другой доступный сервер.",
  unavailable: "Сейчас нет доступного подключения. Обновите статус позже.",
} as const;
```

Use `role="status"` for preparation, `role="alert"` for failed actions, visible label and keyboard-operable controls. Keep the stable URL in component memory only; clear displayed URL and QR on rename/logout/unmount using existing epochs. QR encodes the same stable subscription URL. Present a separately labelled `Прямая ссылка текущего сервера` only after explicit reveal if desired by existing behavior; it is never the primary import URL. No local/session storage, analytics events or external subscription URL conversion.

When a prepared location becomes ready, refresh the device metadata using the existing parent data-load epoch before updating card connectability. Invalidate `dataLoadEpoch` at device mutation start so an older bulk response cannot overwrite a newer rename/location action. Preserve session-generation and rename invalidation checks already in `Portal.tsx`.

- [ ] **Step 4: Extend admin frontend API/types** in `src/api.ts` with publication fields and `updateVpnEndpointPublication`; add the public name/country/published controls to each `CapacityRow` in `VpnEndpointCapacityPanel.tsx`, with their own submit and inline error/status. The capacity form remains separate to avoid changing publication accidentally while saving capacity. UI text: `Название для клиентов`, `Код страны`, `Показывать клиентам`, `Сохранить публикацию`, `Скрытие убирает сервер из нового выбора. Выданные подключения продолжат работать`. Apply returned response through existing `onUpdated`/`replaceVpnEndpointCapacity`.

Add `getVpnSubscriptionDeviceUsage(id)` to the admin client. In `VpnCustomerWorkspacePanel.tsx`, fetch authoritative usage for the selected customer's subscriptions and refresh after create/revoke/subscription edit. Replace all three `countReservedVpnDeviceSlots(selectedAccessKeys, subscription.id)` uses (pre-submit check, form, subscription summary) with the server result. While usage is loading/unavailable, show `Проверяем занятые места` / `Не удалось проверить занятые места`, offer retry and disable creation; do not show partial list counts as exact. Root's backend limit check remains final authority after race/conflict. Reuse request epoch/customer selection guards so a previous customer's usage cannot update a new selection.

- [ ] **Step 5: Add behavioral verification**: browser QA with an actual mounted picker, two devices each having multiple children yet only two cards; successful pending→ready selection; failed preparation retains A and URL; A→B→late-A cannot show A current; selecting an owned ready endpoint at capacity; hidden current displayed but absent from options; logout during GET/POST/QR leaves no private data. Include mobile 390px and desktop, keyboard label association, focus, no overflow. Admin QA must cross the 1000-key cap using authoritative usage and test explicit publication toggling. Mock only local API/clipboard, never call real Telegram or a live node.

- [ ] **Step 6: Run GREEN**: `npm test`, `npm run build`, then existing `test/browser/portal-ui.qa.mjs` and admin QA through the root's existing Playwright runtime path. Extend existing harnesses rather than add a browser dependency. Run affected backend suites once more only if integration exposed a backend issue; commit frontend slice with `feat(vpn): select locations inside one device profile`.

## Release and architecture review gates

- [ ] PostgreSQL migration runs over old schema, metadata-before-migrations startup and repeated migration without changing UUID/expiry/direct URL.
- [ ] PostgreSQL concurrent allocation, same/different location selection and manual revoke tests execute and pass; SQLite is not evidence of locking correctness.
- [ ] Nginx token redaction is deployed and checked before enabling subscription links; application redaction tests include exception records. Existing server-level error sink remains present in every portal-facing vhost.
- [ ] Stable feed decodes to one ready chosen credential; real Happ/Hiddify import/refresh remains a separate device smoke check before rollout. Local encoding tests establish format and selection correctness, not behavior of installed mobile clients.
- [ ] Fresh installations have no published endpoints until the administrator intentionally publishes a named country. Deployment notes must state this service-admission change; do not silently publish current nodes to keep trial allocation green.
- [ ] `traffic_limit_gb` keeps its existing per-remote-key meaning. Extra prepared credentials are summed by the later statistics slice; this plan does not implement aggregate cross-node quota enforcement.

No unresolved product approval is required: device semantics, publication default, old-link preservation and committed-reservation behavior are accepted. Remaining operational gates are the persistent non-default signing secret, isolated PostgreSQL evidence, backup/restore rehearsal and real-client smoke check. Report failures concretely; never bypass capacity, trust or owner validation to finish a test.
