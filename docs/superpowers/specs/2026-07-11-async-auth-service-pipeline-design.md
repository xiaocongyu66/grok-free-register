# Async Local Authentication Pipeline Design

## Goal

Turn the local xAI authentication service into an event-driven pipeline that
continuously drains both historical and newly registered accounts without
placing SSH synchronization, browser startup, or fixed polling sleeps on the
per-account critical path. Preserve the existing CPA-compatible JSON output,
privacy-cookie rejection, isolated browser contexts, ledger idempotency, and
interactive pause/cancel/quit controls.

## Measured Baseline

The current live process shows:

- 12 imports in 14.23 minutes, or 0.84 imports/minute end to end;
- 10.1 seconds average active time for a successful authentication;
- about 34.5 seconds between one completed job and the next job start;
- 30 seconds of that gap is the service loop sleep;
- a full SSH export takes 4.3–5.8 seconds;
- launching Chromium takes 0.27–1.36 seconds; and
- all 15 distinct accounts observed in the current run eventually imported.
  The non-import results were temporary `rate_limited` responses.

The dominant local bottleneck is therefore orchestration, not CPU, memory,
credential serialization, or the OAuth browser interaction itself.

## Architecture

```text
remote snapshot/follow stream
          |
          v
 bounded source queue -- device-flow prefetch (1 ahead)
                              |
                              v
                  global rate-limit gate
                              |
                              v
              authorization worker (concurrency 1)
                         /            \
                        v              v
              next flow prepares   token polling
                                         |
                                         v
                                atomic local sink
                                         |
                                         v
                                  ledger + metrics
```

The service owns these tasks for its entire lifetime. Queues are bounded so a
large historical stock does not retain every account credential in memory.
Only the preparation stages overlap. The browser authorization submission is
initially serialized because current measurements show an upstream rate limit
and provide no evidence that concurrent confirmations increase sustainable
throughput.

## Remote Source Stream

The remote exporter gains a newline-delimited follow mode. It opens the exact
session JSONL once in binary mode, parses only newline-terminated records, and
retains an incomplete final line until more bytes arrive. It emits one complete,
password-free snapshot containing exact sessions plus historical accounts, then
continues reading appends from the same open file descriptor and byte position.
An append between the initial EOF and the follow loop is therefore visible on
the next read; there is no detach/reattach handoff window.

The full snapshot preserves the current legacy fallback: historical
`accounts.txt` entries without an exact snapshot are emitted with SSO Cookie
scope templates learned from exact snapshots. The password field is parsed only
as a delimiter on the server and is never emitted. If no exact snapshot exists
from which a safe Cookie scope can be learned, legacy entries are not guessed.

The local source task keeps one SSH child process open, uses SSH keepalives, and
reconnects after a bounded delay if the stream exits. The remote follower checks
for path inode replacement or truncation; either condition ends that stream so
the local task reconnects and obtains a new complete snapshot. At most one SSH
child is live at a time, although a disconnected child may be replaced.

Every reconnect starts with a complete snapshot. A process-wide keyed state map
tracks each source as `queued`, `prepared`, `active`, `retry_waiting`, or
`imported`. It suppresses duplicate snapshot/follow records across all live
states, while ledger `imported` rows suppress completed work across restarts.
Reconnects therefore require no remote cursor file. The source queue capacity
is 64; normal pipe backpressure bounds secret-bearing records in memory without
affecting the independent registration process.

The exporter never emits account passwords. Terminal output never emits email,
Cookie, SSO, device-code, or OAuth-token values.

## Authentication Pipeline

One persistent headless Chromium process is started when the service starts and
closed only during shutdown. Each account still receives a fresh, isolated
browser context that is closed after its authorization attempt. Privacy choices
click `Reject all`; the later OAuth consent control clicks `Allow`.

The preparation queue capacity is one. Its task requests at most one device flow ahead of the active
authorization. A prepared flow carries its monotonic creation time. It is
discarded and regenerated before use if its remaining issuer lifetime is below
a 60-second safety margin. This overlaps device-code network latency with the
current account without accumulating expiring codes.

After the consent page reports authorization, token polling and the atomic
local sink run in a completion queue of capacity two while preparation of the
next account may continue. A job remains `active` until this completion settles
and is recorded as `imported` only after its complete CPA-compatible JSON file
has been durably replaced into the destination.

Attempt numbers are derived from existing ledger rows for the source
fingerprint. On startup, pending rows are settled as `cancelled` with
`recovered_pending`; the complete source snapshot then re-admits every source
without an imported row. `rate_limited` retries have no fixed attempt limit
while the persistent service is running. Pre-device transport errors,
`browser_error`, confirmation timeout, and sink transport failures retry at most
three times for the same source session with 5, 15, and 30-second delays.
Login-required, explicit OAuth denial/rejection/expiry, invalid source, and
unsafe/unknown pages are terminal for that source session. Operator `c` settles
the active attempt as `cancelled` and places the source at the back of the retry
queue after 60 seconds; it does not immediately select the same source again.

## Rate-Limit Gate

Only an authorization-stage browser result whose reason is `rate_limited` opens
the process-wide authorization gate. No new browser confirmation is submitted
while it is open. The gate waits 60 seconds and then admits exactly one probe.
An `AUTHORIZED` confirmation clears the gate immediately, before token polling
or sink work, and reports the complete elapsed recovery interval. Another
`rate_limited` result re-arms 60 seconds. Account-local browser failure,
transport failure, confirmation timeout, or operator cancellation is an
inconclusive probe: it leaves the gate tripped and re-arms 60 seconds. Token
polling and sink outcomes never open, clear, or re-arm the authorization gate.

Pause does not reset the cooldown clock, but no probe is admitted until resume.
Cancelling an active probe settles that attempt and re-arms the gate; quitting
cancels the gate and all waiters as part of process shutdown. Upstream queues
remain bounded while the gate is closed.

This gate controls only the external confirmation stage. SSH streaming, ledger
queries, and safe device-flow preparation may continue, subject to queue and
expiry bounds. Authorization concurrency remains one until live evidence shows
that a higher value improves completed imports per wall-clock minute rather
than merely increasing rate-limit responses.

## Scheduling and Controls

The fixed post-cycle 30-second sleep and repeated full-snapshot polling are
removed. Work begins immediately when a source record becomes available or a
retry deadline expires. With no work, the consumer blocks on queue/event
conditions and produces no periodic terminal noise. This does not remove the
OAuth protocol's issuer-provided asynchronous token-poll interval; `slow_down`
continues to increase that interval exactly as required by the protocol.

`p` pauses admission to the authorization stage but keeps the source connection
healthy. `r` resumes it. `c` cancels the active authorization/completion job and
settles its ledger entry. `q` closes the source stream, cancels pipeline tasks,
closes Chromium, and exits. `s` reports a point-in-time aggregate without
identifiers or credentials.

## Metrics and User Output

Output remains event-driven. A completed result reports total imports, a
five-minute rolling wall-clock import rate, and eventual account success rate.
Rate limiting reports the wait and the measured recovery interval. The `s`
command reports source/prepared/completion queue depths, active stage,
imported/attempted unique accounts, temporary rate-limit count, five-minute
rate, and process-lifetime effective imports/minute.

Attempt success is imported attempts divided by all finalized attempts.
Eventual account success is unique imported sources divided by unique sources
that have reached an authorization attempt. A temporary rate-limit response
lowers attempt success but does not count as a permanently failed account if a
later retry imports it.

## Persistence and Compatibility

The existing SQLite ledger and local output directory remain authoritative.
The ledger stores only keyed fingerprints and result metadata. Existing
`imported` rows prevent duplicate work after restart. Existing CPA-compatible
JSON documents remain unchanged and directly usable by the current consumer.

No credential, source session, or SSH configuration is added to the repository.
Repository changes are limited to the authentication service, coordinator
lifecycle, remote session exporter, and documentation for the new runtime
behavior.

## Verification

No new permanent test files are required for this change. Verification uses
syntax checks, repository diff checks, one-off bounded diagnostics for queue and
gate invariants, and a real service run. The live acceptance criteria are:

1. one persistent SSH source process and one persistent Chromium process;
2. historical accounts continue to drain and new registrations appear without
   restarting the service;
3. no fixed 30-second gap between successful jobs;
4. privacy Cookie rejection still produces successful imports;
5. a rate-limit event admits no confirmations for 60 seconds and then exactly
   one probe;
6. every output JSON remains valid CPA-compatible data with mode `0600`; and
7. terminal and ledger inspection reveal no raw identifier or credential.

One-off bounded diagnostics additionally exercise an append that occurs between
snapshot EOF and follow-loop entry, an incomplete trailing JSONL record,
snapshot duplicates while a source is queued/active/retrying, and
rate-limit/probe/pause/cancel transitions. They are runtime diagnostics only and
do not add permanent test files.
