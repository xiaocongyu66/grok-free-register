# xAI OAuth Enroller Design

## Goal

Add a standalone, bounded-concurrency enrollment tool to this repository. It
converts an existing Grok SSO session into an xAI OAuth credential that CPA can
store, refresh, rotate, and serve. The tool is an enrollment utility only: it
does not proxy model requests and it does not alter the existing registration
pipeline.

The first production trial is limited to 100 source accounts. The tool must
support a smaller preflight run against one account before a batch is started.

## Constraints

- Existing Grok `sso` values are not valid xAI API bearer credentials and must
  not be imported directly into CPA as xAI credentials.
- xAI OAuth credentials consist of an access token and refresh token. CPA owns
  the persisted credential after import and performs normal token refresh.
- No system or desktop browser may be opened. Browser work is headless and
  isolated from the user's normal browser profile.
- The tool must not attempt to solve, bypass, or replay login, consent,
  multi-factor, or anti-abuse challenges. A job that needs interaction becomes
  a terminal `needs_interaction` result.
- Existing `grok-free-register` functionality and its output files remain
  unchanged.
- The repository may contain unrelated uncommitted work. This feature only
  changes files owned by the enrollment tool and its tests and documentation.

## Boundary

The new package is named `xai_enroller`. Its only external contracts are:

1. Read one source SSO session through a configured source adapter.
2. Use the official xAI OAuth authorization-code flow with PKCE.
3. Import a CPA-compatible xAI auth file through CPA's management API.

It has no runtime import from `grok2api`, no direct write access to CPA's
`auths` directory, and no model-serving endpoint.

### Source adapters

`SourceAdapter` yields opaque source records:

```text
source_id
sso_token
metadata
```

The initial adapters are:

- `file`: one token per line, suitable for controlled small batches.
- `sqlite`: read-only access to a configured existing account database.

The SQLite adapter is deliberately a boundary adapter, not a dependency on the
two API application. It accepts a database path and selection query parameters
through configuration. It never updates the source database.

The local result ledger stores only a salted fingerprint of `source_id`; it
never stores a source token, OAuth access token, refresh token, email address,
or raw OAuth URL.

### OAuth coordinator

`OAuthCoordinator` owns one loopback callback server at
`127.0.0.1:56121/callback`. Every job generates a separate PKCE verifier,
state, and nonce. The callback handler routes a returned authorization code to
the pending job by `state`, allowing several jobs to share the fixed callback
address without sharing credentials.

For each job it:

1. discovers xAI OAuth endpoints;
2. creates the PKCE authorization URL;
3. assigns the state to a pending job;
4. exchanges the returned authorization code;
5. produces an in-memory CPA-compatible xAI credential document.

The credential document remains in memory until the CPA import succeeds. It is
not written to a staging directory.

### Browser runner

`BrowserRunner` starts one headless Chromium process for the process lifetime.
Each enrollment job receives a fresh browser context. Before navigation, the
runner injects only the source account's SSO cookie into that context.

The runner navigates to the official authorization URL and observes the result:

- callback reached: OAuth continues;
- explicit login, consent, MFA, or challenge page: job stops as
  `needs_interaction`;
- navigation or callback timeout: job stops as `timeout`;
- invalid source session: job stops as `source_invalid`.

Contexts are always closed after a job. No persistent browser profile is used.

### CPA importer

`CPAImporter` authenticates to CPA's management API and uploads the
CPA-compatible xAI auth document through the supported auth-file upload
endpoint. It does not mount, copy into, or otherwise modify CPA's auth
directory.

After import, the importer verifies that CPA reports the credential as an xAI
OAuth credential. A configurable optional probe can make one minimal model
request through CPA. The default is disabled so enrollment is not coupled to a
specific model rollout.

## Scheduling and Resource Limits

The process uses an asyncio queue and a fixed `ENROLL_CONCURRENCY` semaphore.
The semaphore covers active browser contexts and OAuth states. It is the only
per-job concurrency control; there is no worker-per-account process model.

Configuration includes:

```text
ENROLL_TARGET=100
ENROLL_CONCURRENCY=4
ENROLL_TIMEOUT_SEC=180
ENROLL_MAX_MEM_MB=1024
ENROLL_MAX_CPU=1.0
```

Container-level CPU and memory limits are deployment controls. The application
also rejects a startup configuration whose requested concurrency exceeds its
static memory budget. The process uses one Chromium instance and at most
`ENROLL_CONCURRENCY` live browser contexts.

The initial default concurrency is `1` for preflight. Operators opt into a
higher value only after the one-account real-flow verification succeeds.

## Result Ledger and Retry Policy

The local SQLite ledger records:

```text
source_fingerprint
attempt_number
state
started_at
finished_at
reason_code
cpa_credential_fingerprint
```

States are:

- `imported`
- `source_invalid`
- `needs_interaction`
- `timeout`
- `oauth_rejected`
- `cpa_import_failed`
- `transport_failed`

Only `transport_failed` and `cpa_import_failed` may be retried automatically,
with a bounded retry count and exponential backoff. All other terminal states
require an explicit operator action or a fresh source credential.

The ledger prevents the same source record from being enrolled twice in a
single run or after restart unless the operator requests a retryable resume.

## CLI and Configuration

The feature is exposed through a separate command:

```text
python -m xai_enroller --source sqlite --target 1
python -m xai_enroller --source sqlite --target 100 --concurrency 4
```

It receives all secrets only through environment variables or an operator-owned
environment file:

```text
XAI_ENROLLER_SOURCE_DB
XAI_ENROLLER_SOURCE_SALT
XAI_ENROLLER_CPA_BASE_URL
XAI_ENROLLER_CPA_MANAGEMENT_SECRET
XAI_ENROLLER_PROXY_URL
```

Configuration validation rejects missing source salts, missing CPA credentials,
non-loopback callback hosts, and unsafe output locations.

## Error Handling

The process fails closed:

- A callback with an unknown or expired state is rejected.
- OAuth state and nonce mismatch are terminal failures.
- CPA import is only attempted after a successful OAuth exchange.
- If CPA import fails, in-memory OAuth material is discarded after the
  retry policy is exhausted.
- Startup and shutdown close the callback server and Chromium process cleanly.
- Logs contain state names, timing, counters, and fingerprints only.

## Testing

Unit tests cover:

- PKCE/state construction and callback routing for concurrent jobs;
- source adapters' read-only behavior and duplicate suppression;
- semaphore, timeout, cancellation, and retry invariants;
- credential redaction in logs and result ledger;
- CPA auth-file request construction and response validation.

Integration tests use a local mock OIDC issuer and mock CPA management API.
They verify multiple jobs can share one callback server and that no credential
is persisted locally after a successful import.

The real preflight is an operator-run, single-account test. Success requires:

1. no system browser was opened;
2. CPA received exactly one xAI OAuth credential;
3. the credential is visible to CPA's auth-file manager;
4. CPA can refresh or use the imported credential according to its normal
   runtime behavior.

Only after this preflight passes will the 100-account trial run.
