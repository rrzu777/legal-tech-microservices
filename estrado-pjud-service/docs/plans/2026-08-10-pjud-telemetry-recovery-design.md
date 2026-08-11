# PJUD Telemetry Recovery Design

## Context

On 2026-08-10 the worker received a transient `httpx.RemoteProtocolError: Server disconnected` while talking to Supabase. A concurrent paid-session refresh could not prove that its budget reservation had persisted, so the existing fail-closed path changed `pjud_proxy_control` to `paused / telemetry_unavailable`. The pause worked as designed, but a single ambiguous transport response stopped all automatic synchronization until manual intervention.

## Decision

Keep the persistent fail-closed circuit and explicit operator reactivation. Before opening that circuit, recover only operations whose outcome can be proven from durable state:

1. Retry transient Supabase transport failures at most three times with short bounded backoff.
2. Reconcile ambiguous reservation responses by the reservation idempotency key and the caller-generated claim token. A different claim token is never adopted.
3. Reconcile ambiguous ledger inserts by reading the immutable event and comparing its complete functional payload. A mismatch fails closed.
4. Reconcile ambiguous finalization by reading the reservation with its claim token and accepting only the requested terminal state.
5. Never retry validation, authorization, constraint, billing, or budget-denial errors.

This is deliberately not automatic circuit recovery. If three attempts cannot prove durable telemetry, `telemetry_unavailable` remains persistent and paid traffic stops.

## Data flow

`ProxyUsageTracker.track()` reserves before provider traffic, records one immutable event afterward, and finalizes the reservation. Each boundary receives a small state-aware recovery function. The functions emit only operation and attempt metadata; they do not log case identifiers, credentials, URLs, document tokens, or payload contents.

## Production acceptance

After tests, review, merge, and deployment:

1. Confirm the worker and proxy-control reads are healthy.
2. Explicitly change the control row from `paused / telemetry_unavailable` to `enabled` with a revision-checked update.
3. Run one existing public non-Familia cause through manual refresh.
4. Verify a successful automatic office-hours cycle advances `next_sync_at`.
5. Re-run the document-contract auditor and confirm zero unsafe payloads, zero direct document URLs, and zero document traffic after deployment.

## Rejected alternatives

- **Blind retries:** unsafe when the server committed but the response was lost.
- **Automatic reactivation:** can resume paid traffic while telemetry is genuinely unavailable.
- **Leaving the current behavior unchanged:** preserves accounting safety but turns a one-off database disconnect into an indefinite operational outage.
