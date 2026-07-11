# Central Release Orchestration Implementation Plan

**Goal:** Make NewAPI and CPA releases operable from central n8n, with automated
build/prepare, one human cutover gate, automatic failure restoration, and
immediate rollback.

**Architecture:** GitHub Actions builds immutable images. n8n coordinates exact
workflow runs and calls service-specific forced-command dispatchers. NewAPI
updates the zero-weight A/B slot and switches the existing HAProxy Runtime API;
CPA retains its existing single-writer A/B controller.

**Excluded:** NewAPI process-role refactoring, duplicate background-task cleanup,
CPA uTLS Transport cache, Nginx changes, and historical NewAPI stacks.

## Task 1: Amend and Lock the Release Specification

**Files:**
- Modify:
  `docs/superpowers/specs/2026-07-11-central-release-orchestration-design.md`

- [x] Preserve the existing two-running-container NewAPI topology.
- [x] Mark duplicate background tasks as non-blocking.
- [x] Remove process-role bootstrap and constrained-SFTP requirements.
- [x] Keep immutable images, serialized prepare/cutover, forced commands, health
  checks, restoration, and manual rollback.
- [ ] Run document consistency checks and commit the amendment.

## Task 2: Implement the NewAPI Deployment Dispatcher With TDD

**Files:**
- Create: `ops/new-api-release/rtoc-newapi-deploy`
- Create: `ops/new-api-release/rtoc-newapi-deploy-dispatch`
- Create: `tests/test_newapi_release.py`
- Create: `ops/new-api-release/README.md`

- [ ] Write failing tests for forced-command parsing and rejection.
- [ ] Implement only the allowlisted command wrapper.
- [ ] Write failing tests for active/inactive slot discovery.
- [ ] Implement read-only JSON `status`.
- [ ] Write failing tests for candidate-only compose image replacement.
- [ ] Implement `prepare` with immutable digest, image/revision verification,
  compose backup, candidate recreation, and direct health.
- [ ] Write failing tests for cutover and failure restoration.
- [ ] Implement HAProxy weight switching with saved-weight trap.
- [ ] Write failing tests for immediate rollback and rollback restoration.
- [ ] Implement rollback to the recorded previous slot.
- [ ] Run focused and complete local tests.

## Task 3: Install the NewAPI Dispatcher Without Cutting Traffic

**Production target:** `rtoc-prod-relay`

- [ ] Copy versioned scripts to `/usr/local/libexec`.
- [ ] Create `/srv/new-api-ai/.rtoc-release` state/evidence directory.
- [ ] Create a dedicated Netcup-origin forced-command SSH key.
- [ ] Restrict the key to `status/prepare/cutover/rollback`.
- [ ] Verify arbitrary shell, extra arguments, and malformed digests are rejected.
- [ ] Execute `status` directly and confirm current B-active/A-inactive state.
- [ ] Do not execute production cutover.

## Task 4: Extend NewAPI GitHub Actions Build Output

**Repository:** `/Users/rtoc/Documents/WorkSpace/new-api-rtoc-backend`

**Files:**
- Modify: `.github/workflows/rtoc-backend-image.yml`
- Create: `.github/scripts/release-manifest.sh`
- Create or modify focused workflow validation tests/scripts.

- [ ] Add `workflow_dispatch` inputs `commit_sha` and `build_request_id`.
- [ ] Set a unique run name containing `build_request_id`.
- [ ] Check out the exact requested commit.
- [ ] Run the repository's established test suite before image push.
- [ ] Resolve the pushed `linux/amd64` digest.
- [ ] Emit and upload the release manifest.
- [ ] Validate workflow syntax and manifest generation locally.
- [ ] Commit and push the workflow change.
- [ ] Trigger one non-production build and verify manifest/image identity.

## Task 5: Replace Historical NewAPI n8n Drafts

**Central host:** `netcup`

- [ ] Export all existing workflows and credentials for encrypted backup.
- [ ] Import a NewAPI forced-command SSH credential.
- [ ] Create disabled workflows:
  - `RTOC New API / 00 Status`
  - `RTOC New API / 01 Build and Prepare`
  - `RTOC New API / 02 Cut Over Prepared Candidate`
  - `RTOC New API / 03 Roll Back`
- [ ] Bind Status and deployment nodes to the new restricted SSH credential.
- [ ] Add a repository-scoped GitHub Actions credential or an equivalent
  restricted trigger/read path.
- [ ] Ensure Build and Prepare accepts an exact commit, waits for the correlated
  workflow run, validates manifest/digest, and calls `prepare`.
- [ ] Ensure Cut Over has only a manual trigger and requires `confirm=CUTOVER`.
- [ ] Ensure Roll Back has only a manual trigger and requires
  `confirm=ROLLBACK`.
- [ ] Keep all workflows disabled during construction.

## Task 6: Exercise NewAPI Read-only and Prepare Paths

- [ ] Execute Status end to end from n8n.
- [ ] Run a no-op prepare using the image already present in the inactive slot,
  or a freshly built candidate, without changing HAProxy weights.
- [ ] Verify the active public endpoint remains on the original slot.
- [ ] Verify candidate direct health and recorded release identity.
- [ ] Stop at the human cutover approval gate.

## Task 7: Complete CPA Build Integration

**Repository:** `/Users/rtoc/Documents/WorkSpace/cliproxyapi-rtoc`

- [ ] Create private `hechuyi/cliproxyapi-rtoc` if still absent.
- [ ] Push the reviewed RTOC compatibility branch.
- [ ] Add an amd64 GHCR workflow with exact commit, tests, digest, and manifest.
- [ ] Extend the existing CPA Prepare workflow to consume the build result rather
  than a hard-coded local image.
- [ ] Preserve the deployed CPA controller and forced-command key.
- [ ] Execute CPA Prepare end to end without cutover.
- [ ] Stop at the human cutover approval gate.

## Task 8: Production Cutover and Rollback Gates

- [ ] Present prepared NewAPI and CPA release identities, source/target slots,
  image digests, and health evidence.
- [ ] Obtain explicit user approval for each production cutover.
- [ ] Execute cutover through n8n only.
- [ ] Verify stable local and public health.
- [ ] Exercise one controlled rollback per service and record evidence.
