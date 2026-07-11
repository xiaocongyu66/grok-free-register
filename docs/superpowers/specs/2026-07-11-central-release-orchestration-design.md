# Central Release Orchestration for NewAPI and CPA

Last updated: 2026-07-11 Asia/Shanghai

## Objective

Use the Netcup n8n instance as the central release controller for:

- the primary NewAPI production stack at `/srv/new-api-ai`;
- the RTOC CPA compatibility service;
- automated build, test, immutable image resolution, candidate preparation, and
  health checks;
- one human approval immediately before production traffic cutover;
- automatic restoration when cutover smoke fails;
- a human-triggered rollback to the immediately retained previous release.

Code changes enter the workflow as reviewed Git commits. n8n orchestrates a
release; it does not generate or edit production code over SSH.

The CPA uTLS Transport cache and historical NewAPI stacks are outside this
implementation.

## Confirmed Runtime Model

NewAPI currently runs two complete application containers:

```text
A -> 127.0.0.1:3012
B -> 127.0.0.1:3013
stable HAProxy entry -> 127.0.0.1:3010
public domain -> api.rtoc.cc
```

Both containers stay running. HAProxy gives one slot weight 100 and the other
weight 0. Weight zero prevents new public requests from entering that slot but
does not suspend its process.

This is intentional for fast rollback and low-interruption releases. The
release controller must not stop or update the active slot while preparing a
candidate.

## Duplicate Background Tasks

Because both NewAPI containers are complete processes, both currently start
some periodic background tasks. Production observation shows the inactive slot
has low resource usage but still performs channel synchronization and automatic
channel tests.

This behavior is not a blocker for release automation:

- the additional CPU and memory cost is small;
- there is no current evidence that duplicate background tasks are causing a
  production incident;
- splitting Web and worker roles would materially expand the change surface.

The behavior is therefore `PARKED_NON_BLOCKING`. Revisit it only if logs or
database evidence show harmful duplicate writes, repeated external consumption,
or lock contention. The current release project does not change it.

## Release Invariants

1. NewAPI keeps the single stable endpoint `127.0.0.1:3010`.
2. Routine releases do not edit or reload Nginx.
3. NewAPI image updates affect only the zero-weight slot.
4. Deployment commands are serialized by a host-local lock.
5. A candidate is identified by an immutable OCI digest and source commit.
6. Candidate health is verified directly before traffic changes.
7. Cutover changes HAProxy Runtime API weights only after candidate preparation.
8. The previous slot remains running at weight zero after successful cutover.
9. Cutover failure restores the exact previous weights before returning.
10. Only production cutover requires human approval.
11. NewAPI and CPA use separate credentials, state, locks, and dispatchers.
12. No workflow contains unrestricted production shell logic.

## Release State

Each service maintains a small host-local state file and append-only evidence
log. NewAPI records:

```text
active slot
active release and image identity
prepared slot
prepared release and image identity
previous/rollback release identity
pre-cutover HAProxy weights
last operation and result
timestamps
```

State-changing commands use `flock` and atomic file replacement. A second
prepare or cutover fails while another deployment operation holds the lock.

The logical states are:

```text
REQUESTED
  -> BUILDING
  -> BUILT
  -> VERIFIED
  -> PREPARING
  -> PREPARED
  -> AWAITING_APPROVAL
  -> CUTTING_OVER
  -> ACTIVE
```

Failures before cutover leave traffic unchanged. Failures during cutover restore
the previous active slot and end in `FAILED_RESTORED` or `RESTORE_FAILED`.

## GitHub Build Contract

The existing NewAPI GitHub Actions image workflow is extended to accept:

```text
commit_sha
build_request_id
```

The trusted workflow definition checks out the exact requested commit, runs the
configured test suite, builds `linux/amd64`, pushes the image, and emits:

```json
{
  "schema_version": 1,
  "service": "newapi",
  "build_request_id": "...",
  "commit_sha": "40-character commit SHA",
  "workflow_run_id": 123,
  "workflow_attempt": 1,
  "image": "ghcr.io/hechuyi/new-api-rtoc",
  "image_digest": "sha256:...",
  "platform": "linux/amd64",
  "tests": "passed",
  "built_at": "RFC3339 timestamp"
}
```

The production identity is always:

```text
ghcr.io/hechuyi/new-api-rtoc@sha256:<digest>
```

Mutable tags such as `latest` may be published for convenience but are never
accepted by the deployment dispatcher.

CPA receives the same build contract after its private RTOC repository and GHCR
workflow are created.

## Production Dispatcher

n8n reaches each service through a dedicated SSH private key. The corresponding
`authorized_keys` entry enforces:

- Netcup source IP allowlist;
- forced command;
- no PTY, port, agent, or X11 forwarding;
- no arbitrary shell.

The wrapper accepts n8n's fixed `cd / ; COMMAND` prefix and only these commands:

```text
status
prepare <immutable-image> <image-id> <source-revision> <release-id>
cutover <release-id>
rollback
```

All arguments use strict allowlists. `prepare` accepts only the configured image
repository with an OCI SHA-256 digest. The dispatcher returns machine-readable
JSON and never returns environment variables or registry credentials.

The versioned deployment program contains the release mechanics. n8n nodes only
validate operator input and invoke one of the commands above.

## NewAPI Prepare

`prepare` performs the following serial operation:

1. acquire the NewAPI deployment lock;
2. read HAProxy status and identify the weight-100 active slot;
3. select the weight-0 slot as the candidate;
4. verify the other slot is healthy and serving the stable endpoint;
5. pull the immutable candidate digest;
6. verify platform, image ID, and source revision label;
7. back up `docker-compose.yml`;
8. change only the candidate service image;
9. recreate only the candidate container;
10. verify direct candidate `/api/status`;
11. verify the stable endpoint still uses the old active slot;
12. persist the prepared release and rollback identity.

The active slot is never recreated by `prepare`.

Because A and B share Postgres and Redis, the normal workflow accepts only
application releases whose startup migration is absent or backward compatible
with the active release. A release requiring a destructive or rollback-unsafe
database migration is rejected from this routine A/B path.

## NewAPI Cutover

`cutover` requires:

- an exact prepared release ID;
- the candidate container still running the prepared image ID;
- the source/active slot and HAProxy weights still matching preflight state.

The operation:

1. acquire the deployment lock;
2. save the exact current weights;
3. set the candidate slot weight to 100;
4. set the previous active slot weight to 0;
5. verify both HAProxy weights and candidate health;
6. check the stable local endpoint and public status endpoint;
7. persist the new active and previous release identities.

A shell trap restores the saved weights if any step fails before state commit.
The previous container remains running, so restoration does not require image
pulling or container recreation.

Routine cutover does not touch Nginx or rebuild the NixOS ingress configuration.

## NewAPI Rollback

`rollback` targets only the immediately retained previous slot. It verifies that
the slot still runs the recorded image, changes its weight to 100, changes the
current slot to 0, runs stable and public health checks, and restores the
pre-rollback weights if validation fails.

An older release that is no longer present in either slot must be prepared as a
new candidate. It is not an immediate rollback.

## CPA

CPA keeps its existing production A/B controller and four central workflows:

```text
RTOC CPA / 00 Status
RTOC CPA / 01 Prepare Candidate
RTOC CPA / 02 Cut Over Prepared Candidate
RTOC CPA / 03 Roll Back
```

Only Status has been exercised end to end through n8n. The remaining workflows
must be tested against the existing forced-command dispatcher before being
treated as production-ready.

CPA maintains its current single-writer behavior because both slots share
mutable auth and configuration directories. This differs from NewAPI's
two-running-container model but uses the same operator-facing state sequence.

## n8n Workflows

Each service exposes four operator workflows:

### Status

Read-only status of active slot, prepared release, image identities, health, and
last deployment result.

### Build and Prepare

1. accept an exact Git commit;
2. create `build_request_id`;
3. trigger the service GitHub Actions workflow;
4. identify and wait for the exact matching run;
5. verify tests, commit, platform, and image digest;
6. call production `prepare`;
7. run direct candidate checks;
8. stop at `AWAITING_APPROVAL`.

Build, verification, image pull, and candidate recreation are automatic.

### Cut Over Prepared Candidate

This workflow has no public webhook. It is manually started by an authenticated
n8n operator and requires the exact prepared release plus `confirm=CUTOVER`.
It rechecks status, calls `cutover`, and records the result.

### Roll Back

Manually started with `confirm=ROLLBACK`. It calls the service rollback command,
verifies the stable/public endpoint, and records evidence.

The eleven historical NewAPI workflow drafts are exported for audit and
replaced. They are not repaired by changing paths or attaching credentials.

## Credentials

Credentials are separated by purpose:

- GitHub Actions trigger/read credential;
- NewAPI forced-command SSH credential;
- CPA forced-command SSH credential;
- host-local read-only GHCR pull credential;
- optional dedicated smoke credentials.

n8n does not receive application `.env`, database passwords, or unrestricted
root SSH access. GitHub credentials are limited to the two release repositories
and required Actions metadata.

## Verification

Dispatcher tests cover:

- command and argument rejection;
- immutable digest enforcement;
- active/inactive slot detection;
- changing only the inactive compose service;
- image identity checks;
- prepare failure with unchanged active traffic;
- cutover success;
- cutover failure with restored weights;
- rollback success and rollback failure restoration;
- idempotent repeated status and release commands;
- secret-free JSON output.

n8n workflow tests cover:

- mismatched commit or workflow run rejection;
- failed build/test stopping before production SSH;
- prepare leaving traffic unchanged;
- cutover requiring explicit manual confirmation;
- rollback requiring explicit manual confirmation.

Production rollout order:

1. install NewAPI dispatcher and forced-command key;
2. execute NewAPI Status from n8n;
3. test forbidden command rejection;
4. prepare the existing inactive image as a no-op validation;
5. import replacement NewAPI workflows as disabled;
6. test Build and Prepare without traffic change;
7. wait for explicit human approval before the first production cutover;
8. exercise and record one controlled rollback after successful cutover.

CPA follows the same validation order using its existing controller.
