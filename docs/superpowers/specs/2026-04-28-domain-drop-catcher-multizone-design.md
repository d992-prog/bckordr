# Domain Drop Catcher Multizone Design

**Date:** 2026-04-28

## Goal

Transform the current codebase from a legacy domain-checker-derived project into a multizone distributed drop-catcher platform with a control server, many worker servers, zone strategies, phase-based execution, and centralized runtime orchestration.

## Product Shape

- `Control Server` is the single source of truth.
- `Worker Server` is a deploy-and-forget executor.
- The system stores all domains, workers, strategies, accounts, contacts, events, and runtime history on control.
- The system does not check availability before registration attempts.
- The system attacks inside configured zone windows using registrar APIs.
- Success on one domain stops further attempts for that domain and immediately frees workers to help remaining domains.

## Scope Decisions

- The architecture is multizone, not `.fr`-only.
- `.fr` is the first preconfigured zone strategy, not a special-case system architecture.
- Rules for all zones are manually configurable in the control panel.
- A zone may contain multiple rules and multiple execution windows.
- Rule resolution supports:
  - `priority`
  - `merge`
- A rule may use:
  - `flat` execution
  - `phased` execution
- A phase may define its target in:
  - percent of available domain capacity
  - fixed RPS

## Core Entities

### RegistrarAccount

Stores registrar credentials and validation status. The system is multi-account-ready, but the initial deployment uses one Gandi account.

### ContactProfile

Stores registrant data. The initial deployment uses one default profile that is auto-assigned to new domains.

### ZoneStrategy

Defines per-zone defaults:

- zone
- timezone
- rule resolution mode
- default minimum guaranteed RPS
- default registrar slug

### ZoneRule

Defines when a zone may be attacked:

- hourly
- daily
- weekly
- one-time

Each rule has window timing, priority, and execution mode.

### ZoneRulePhase

Defines per-window execution phases:

- prefire
- burst
- sustain
- tail

Each phase has an offset, duration, and either percent-based or fixed-RPS targeting.

### DropDomain

Stores the domain, drop date, priority, status, assigned defaults, and strategy mode:

- `inherit_zone`
- `manual_override`

### DomainRuleOverride

Stores per-domain manual strategy overrides when a domain does not inherit the zone strategy.

### WorkerNode

Represents one VPS/IP-bound executor with:

- registrar compatibility
- account compatibility
- status
- target/max/current RPS
- CPU/RAM/time-drift metrics

### AttackRun

Represents one planned or active attack window for one domain.

### WorkerTask

Represents one worker assignment inside one attack run.

### AttackEvent

Stores operational runtime history and human actions.

## Runtime Model

### Scheduling Engine

For each domain, control resolves:

1. strategy source
2. matching rules
3. active or next window
4. active execution phase
5. current desired RPS and next recompute point

Domains attack only on their configured `drop_date` in the timezone of the active strategy. Repeating rules exist, but a domain participates only on its active drop day.

### Allocation Engine

Control computes capacity allocation using:

1. minimum guaranteed RPS for every active domain
2. remaining capacity distributed by weighted priority
3. immediate rebalance after success, worker loss, or phase changes

Allocation respects:

- worker online/enabled state
- registrar/account compatibility
- worker target RPS
- worker max RPS
- one-worker-one-domain assignment at any instant

### Worker Runtime

Worker flow:

1. heartbeat
2. fetch task
3. acknowledge
4. wait until start
5. execute async registration requests
6. report progress
7. report success/failure

Workers are intentionally "thin" and do not own zone logic.

## UI Structure

The control panel contains:

- Dashboard
- Domains
- Zone Strategies
- Workers
- Accounts
- Contact Profiles
- Attacks / Runtime
- Events / Logs
- Settings

The registration screen remains visible but disabled. The application operates in single-operator mode for now.

## Status Model

### Domain

- `draft`
- `ready`
- `scheduled`
- `attacking`
- `success`
- `paused`
- `failed`

### Worker

- `provisioning`
- `ready`
- `busy`
- `offline`
- `disabled`

### AttackRun

- `planned`
- `running`
- `success`
- `stopped`
- `failed`

### WorkerTask

- `queued`
- `running`
- `success`
- `failed`
- `cancelled`
- `stopped`

## Implementation Strategy

The current codebase still contains legacy checker-era routes, models, and tests. The migration should happen in controlled foundation-first stages:

1. remove remaining checker behavior from active runtime paths
2. add multizone strategy data model
3. implement effective strategy resolution
4. implement window and phase scheduling
5. implement capacity allocation and rebalance
6. upgrade the control UI to expose the new model
7. keep worker deployment simple and centralized through control-issued tasks

## Constraints

- All tactical configuration lives on control.
- Workers must remain easy to provision and forget.
- Per-worker/IP registrar rate limits are never exceeded.
- The system must support future registrars without redesigning the runtime model.
