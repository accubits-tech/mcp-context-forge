# MCP Triggers & Events — Scenario Catalog

> **Status:** Draft for review &nbsp;·&nbsp; **Date:** 2026-05-30 &nbsp;·&nbsp; **Companion to:** [`frd-mcp-triggers-events.md`](./frd-mcp-triggers-events.md)
>
> Exhaustive catalog of scenarios the triggers/events subsystem must handle, grounded in real provider behavior (GitHub, Stripe, Slack, Shopify, Twilio, GitLab, PagerDuty), webhook-reliability practice (Hookdeck, Svix, AWS EventBridge/SNS, Redis Streams), OWASP/CWE security guidance, pub-sub/CEL routing (Knative, CloudEvents, Google Pub/Sub), and the MCP Tasks/`#523`/notifications specs. Each scenario maps to FRD requirement IDs (`FR-*`, `NFR-*`, `R-*`); gaps with no covering requirement are consolidated in §7 as proposed new requirements. The matching test cases live in [`mcp-triggers-events-test-cases.md`](./mcp-triggers-events-test-cases.md).

## How to read this document

- **ID scheme:** `SC-<CAT>-NNN` where `<CAT>` ∈ {`ING` ingestion, `DEL` delivery/bus, `SEC` security, `SUB` subscription/routing, `COR` correlate/async, `MCP` MCP-native}. IDs are stable; test cases reference them.
- **Severity:** ingestion/delivery/subscription/correlate/MCP use **Critical / Major / Minor**; security uses **Critical / High / Medium / Low** (impact-weighted likelihood). Treat Critical≈Critical, Major≈High, Minor≈Medium/Low when comparing across categories.
- **FRD refs:** best-effort mapping to the FRD; `—, propose FR` means no current requirement covers it (see §7).

| Category | Prefix | Scenarios | Drives milestones |
|----------|--------|-----------|-------------------|
| Inbound ingestion & normalization | ING | 61 | M1 |
| Delivery, ordering & Redis-Streams bus | DEL | 77 | M2, M3 |
| Security & abuse | SEC | 55 | M1–M5 (cross-cutting) |
| Subscription lifecycle, routing/CEL & fan-out | SUB | 57 | M2 |
| Async tool / correlate mode (Tasks + #523) | COR | 27 | M7 |
| MCP-native notifications | MCP | 23 | M6 |

---

## 1. ING — Inbound ingestion & normalization

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-ING-001 | GitHub HMAC-SHA256 hex verify | POST with `X-Hub-Signature-256: sha256=...` | Strip prefix, HMAC raw bytes, constant-time compare, accept | Critical | FR-8, FR-33, R-2 |
| SC-ING-002 | GitHub legacy SHA-1 verify | POST with `X-Hub-Signature: sha1=...` only | Descriptor selects sha1 recipe; verify; accept (deprecated) | Major | FR-8, R-2 |
| SC-ING-003 | Stripe timestamped sig | `Stripe-Signature: t=,v1=`; sign `{t}.{body}` | Validate v1 only, ignore v0; accept | Critical | FR-8, FR-9, R-2 |
| SC-ING-004 | Stripe multiple v1 (rotation) | Header has 2+ `v1=` during secret rotation | Accept if any v1 matches a configured secret | Major | FR-8, NFR-8, R-2 |
| SC-ING-005 | Slack v0 signing | `v0:{ts}:{body}` base string, `X-Slack-Signature` | HMAC v0 recipe; constant-time; accept | Critical | FR-8, FR-9, R-2 |
| SC-ING-006 | Shopify base64 HMAC | `X-Shopify-Hmac-Sha256` base64 over body w/ app secret | base64 encoding recipe; verify; accept | Critical | FR-8, R-2 |
| SC-ING-007 | Twilio HMAC-SHA1+base64 | Sign full URL + sorted POST params, `X-Twilio-Signature` | URL+sorted-params recipe (plugin); verify; accept | Major | FR-6, FR-8, R-2 |
| SC-ING-008 | Twilio JSON bodySHA256 | JSON body uses `bodySHA256` query param | Verify URL sig + body hash; accept | Major | FR-6, FR-8 |
| SC-ING-009 | PagerDuty v3 rotation | `X-PagerDuty-Signature: v1=a,v1=b` CSV | Accept if any v1 matches any active secret | Major | FR-8, NFR-8, R-2 |
| SC-ING-010 | GitLab plaintext token | `X-Gitlab-Token` plaintext compare | `none`/token recipe; constant-time eq; accept | Major | FR-8, FR-33 |
| SC-ING-011 | GitLab newer HMAC | GitLab HMAC signature header | hmac recipe; verify; accept | Minor | FR-8 |
| SC-ING-012 | hex-vs-base64 encoding mismatch | Descriptor encoding ≠ provider encoding | Reject (no match); 401; log encoding error | Major | FR-8, R-2 |
| SC-ING-013 | Non-constant-time compare risk | Timing-attack on signature compare | All compares MUST be constant-time | Critical | FR-8, R-2 |
| SC-ING-014 | Raw-body mutation before verify | Body parsed/re-serialized pre-verify | Verify over exact raw bytes captured pre-parse | Critical | FR-8, R-2 |
| SC-ING-015 | Wrong-secret / multi-endpoint resolution | conn-id resolves to wrong signing secret | Resolve secret by conn-id; mismatch → 401 | Major | FR-7, FR-33, NFR-8 |
| SC-ING-016 | Slack url_verification handshake | `{type:url_verification, challenge}` | Verify sig first, then echo `challenge`; 200 | Major | FR-10, FR-33, R-9 |
| SC-ING-017 | GitHub ping event | `X-GitHub-Event: ping` | 2xx ack; not emitted as domain event | Minor | FR-10, FR-11 |
| SC-ING-018 | GET/HEAD validation probe | Provider sends GET/HEAD to ingress URL | Respond 200 (reachability) w/o emitting event | Minor | FR-7, FR-10 |
| SC-ING-019 | Endpoint reachability at registration | Provider requires URL reachable on subscribe | Ingress route live + answers probe before sub completes | Major | FR-7, FR-17 |
| SC-ING-020 | GitHub redelivery dedup | Same `X-GitHub-Delivery` redelivered | Dedup on `(conn,event.id)`; drop duplicate | Major | FR-23, NFR-4 |
| SC-ING-021 | Stripe duplicate event id | Same Stripe `id` delivered twice | Dedup; process once | Major | FR-23, NFR-4 |
| SC-ING-022 | Stripe twin events | Two events, same `type`+`object.id` | Dedup on type+object.id heuristic | Minor | FR-23, NFR-4 |
| SC-ING-023 | Slack event_id dedup | Repeat `event_id` (+`X-Slack-Retry-Num`) | Dedup on event_id; honor retry header | Major | FR-23, NFR-4 |
| SC-ING-024 | Shopify webhook-id dedup | Repeat `X-Shopify-Webhook-Id` | Dedup; process once | Major | FR-23, NFR-4 |
| SC-ING-025 | Shopify shared event-id fan-out | Multiple topics share `X-Shopify-Event-Id` | Dedup key includes topic to avoid false dedup | Major | FR-23, NFR-4 |
| SC-ING-026 | Dedup TTL too short | Cache TTL < provider retry window | TTL MUST exceed max provider retry window | Major | FR-23, NFR-6 |
| SC-ING-027 | No dedup id present | Provider sends no event id | Derive dedup key from body+headers hash | Minor | FR-23, R-10 |
| SC-ING-028 | Double-processing on slow ACK | ACK slow → provider retries mid-process | Idempotent reprocess; no dup side-effects | Major | FR-12, FR-23, NFR-4 |
| SC-ING-029 | Out-of-order delivery | Provider gives no order guarantee | Accept any order; ordering resolved downstream | Major | FR-11, NFR-3, R-11 |
| SC-ING-030 | Missed events / gaps | Provider drops/never delivers events | Reconcile via provider API (out-of-band) | Major | NFR-6, —, propose FR |
| SC-ING-031 | Stale state from old event | Late event older than current state | Use version/`updated_at`; ignore stale | Minor | FR-11, R-11 |
| SC-ING-032 | Provider retries on non-2xx | Ingress returns 5xx/non-2xx (incl Shopify 3xx) | Provider retries; gateway idempotent on redelivery | Major | FR-12, FR-23, NFR-4 |
| SC-ING-033 | Response-timeout retries | Ingress slower than provider window (GH 10s/Shopify 5s,1s/Slack 3s) | Fast verify+202 within window; async work after | Critical | FR-12, NFR-2, NFR-1 |
| SC-ING-034 | Provider-specific retry windows | Stripe ~3d, Shopify ~4h/8, GitLab 4 then 40 | Dedup TTL + DLQ tuned per provider window | Major | FR-23, FR-36 |
| SC-ING-035 | Auto-disable after sustained failure | Provider disables endpoint after repeated fail | Avoid by fast 202; alert on rising reject rate | Major | FR-39, NFR-7 |
| SC-ING-036 | 429 / retryable from gateway | Gateway under load returns 429/503 | Provider honors Retry-After; resumes later | Major | NFR-1, FR-12 |
| SC-ING-037 | Re-enable resumes retries | Endpoint re-enabled after disable | Backlog/retries resume; idempotent ingest | Minor | FR-23, NFR-4 |
| SC-ING-038 | Replay attack (stale ts) | Captured request replayed later | hmac_timestamped rejects outside tolerance | Critical | FR-9, R-2 |
| SC-ING-039 | Stripe 5-min tolerance | ts within/outside 300s window | Configurable tolerance, never 0; reject stale | Major | FR-9 |
| SC-ING-040 | Slack 300s tolerance | ts older than 300s | Reject as replay | Major | FR-9 |
| SC-ING-041 | Server clock skew | Gateway clock drift vs provider ts | NTP-synced; tolerance absorbs small skew | Minor | FR-9, —, propose FR |
| SC-ING-042 | IP allowlist defense-in-depth | POST from non-provider IP | Optional IP allowlist reject before/with verify | Minor | FR-33, R-3 |
| SC-ING-043 | HTTPS/TLS enforcement | Inbound over plain HTTP | Enforce TLS; reject/redirect insecure | Major | FR-33, —, propose FR |
| SC-ING-044 | Malformed JSON body | Body fails JSON parse | Verify sig FIRST, then 400; no retry loop | Major | FR-11, FR-12 |
| SC-ING-045 | Content-type variations | json vs form-encoded vs `+charset` | Parse per declared content-type; normalize | Minor | FR-11 |
| SC-ING-046 | Payload size limit/truncation | Body > limit (GitHub 25MB) | Enforce max size; reject oversize; no truncation | Major | FR-11, R-3 |
| SC-ING-047 | Unknown / new event type | Type not in taxonomy | 200/202 + ignore (no match), don't error | Minor | FR-11, FR-20 |
| SC-ING-048 | Unexpected action subtype | Known type, new `action` subtype | Normalize; pass through; no crash | Minor | FR-11 |
| SC-ING-049 | API-version skew | Provider API-version header differs | Normalize tolerant of version; record version | Minor | FR-11, —, propose FR |
| SC-ING-050 | Thin vs snapshot payloads | Thin event lacking full object | Accept; downstream fetches full via API | Minor | FR-11, —, propose FR |
| SC-ING-051 | Empty / null body | POST with empty or `null` body | Verify; if no body → 400 (or 200 for probe) | Minor | FR-11, FR-12 |
| SC-ING-052 | Fast ACK + async processing | Any accepted event | Verify+202 immediately; queue async work | Critical | FR-12, NFR-1, NFR-2 |
| SC-ING-053 | Thundering herd / burst | Large simultaneous event burst | L2 stream absorbs; no ingress backpressure | Major | NFR-1, FR-24 |
| SC-ING-054 | Backpressure on ingest | Bus/DB saturated | Return 429/503 fail-safe; provider retries | Major | NFR-1, FR-12 |
| SC-ING-055 | Dead-letter handling | Event un-processable after retries | Route to `dead_letters`; redact secrets | Major | FR-36, FR-27 |
| SC-ING-056 | Missing/invalid signature header | No/garbled signature header | Fail closed: 401 (missing-auth) / 400 (malformed) | Critical | FR-8, FR-33, R-2 |
| SC-ING-057 | Secret rotation overlap | Old+new secrets both active | Accept match on either during overlap window | Major | NFR-8, FR-8 |
| SC-ING-058 | Duplicate / mixed-case headers | Repeated or case-variant sig headers | Case-insensitive lookup; defined dup precedence | Minor | FR-8 |
| SC-ING-059 | Proxy/URL rewrite breaks sig | Reverse proxy alters URL/body | Verify over original URL+raw body; doc proxy rules | Major | FR-8, R-2 |
| SC-ING-060 | CSRF / body-parser interference | Middleware consumes/mutates body | Capture raw body before any parser/CSRF middleware | Critical | FR-8, R-2 |
| SC-ING-061 | Normalize to envelope | Verified, parsed POST | Map to `event{id,source,type,subject,time,data}` reverse-DNS | Critical | FR-11 |

---

## 2. DEL — Delivery, ordering & the Redis-Streams bus

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-DEL-001 | At-least-once is the only honest contract | Any delivery path | Document at-least-once; never promise exactly-once-on-wire; pair with idempotent receiver | Major | NFR-4, FR-26, R-11 |
| SC-DEL-002 | Retry-storm duplicates | Transient receiver flap causes many retries | All carry same Idempotency-Key=delivery_id; receiver dedupes; no duplicate effect | Major | NFR-4, FR-26 |
| SC-DEL-003 | Late duplicate outside dedup window | Re-delivery after receiver's in-memory window | Persistent processed table keyed by delivery_id, TTL beyond retry horizon | Major | FR-26, NFR-4 |
| SC-DEL-004 | Concurrent double-process race | Same delivery_id on two workers simultaneously | Unique constraint / INSERT…ON CONFLICT makes second a no-op | Major | FR-26, NFR-4 |
| SC-DEL-005 | 2xx returned but receiver didn't persist | Receiver acks then crashes before commit | Gateway treats as success (status is truth); receiver must commit-then-ack | Minor | FR-28 |
| SC-DEL-006 | Provider duplicates with different ids | Upstream sends same business event w/ distinct id | Wire dedup misses; receiver does semantic dedup on business key | Minor | FR-23, FR-26 |
| SC-DEL-007 | Naturally idempotent operation | Target op is idempotent (PUT absolute state) | No dedup needed; duplicates harmless; document as preferred | Minor | NFR-4 |
| SC-DEL-008 | Multiple intentional subscriptions | Two subs match same event by design | Each gets own delivery_id/queue; not duplicates | Minor | FR-25, FR-19 |
| SC-DEL-009 | Missing-ack after successful processing | Receiver did work but 2xx lost (conn reset) | Gateway retries; receiver dedupes; advise send 2xx fast | Major | NFR-4, FR-26 |
| SC-DEL-010 | Stable idempotency key across retries | Event created, multiple delivery attempts | delivery_id generated once, never regenerated per attempt | Critical | FR-26, NFR-4 |
| SC-DEL-011 | Dedup-store growth / cleanup | Processed-key table grows unbounded | TTL/eviction beyond retry horizon; bounded memory | Minor | FR-26, NFR-6 |
| SC-DEL-012 | Financial double-fulfillment | Payment/charge event delivered twice | Idempotency-Key prevents double charge; absolute amounts | Critical | FR-26, NFR-4 |
| SC-DEL-013 | Delta-vs-absolute drift | Duplicate/out-of-order delta events skew state | Prefer absolute values or re-fetch authoritative state | Major | NFR-3, NFR-4 |
| SC-DEL-014 | Idempotency key collision/reuse | delivery_id collides across subs/types | Namespace key per subscription+type; collision-resistant id | Major | FR-26 |
| SC-DEL-015 | Exponential backoff schedule | Transient delivery failure | Retry on growing intervals up to max | Major | FR-27, FR-38 |
| SC-DEL-016 | Thundering-herd synchronized retries | Many subs fail at once, retry in lockstep | Randomized jitter on backoff to desynchronize | Major | FR-27, NFR-1 |
| SC-DEL-017 | Max attempts / give-up | Retries exhausted | Move to `dead_letters` + alert; no silent drop | Critical | FR-27, FR-36, FR-38 |
| SC-DEL-018 | Retry budget / rate cap per endpoint | One endpoint consuming all retry capacity | Per-endpoint retry rate cap; isolate so others unaffected | Major | NFR-7, FR-25 |
| SC-DEL-019 | 4xx permanent vs transient classification | Receiver returns 4xx | 4xx permanent (fewer/no retries); 410 auto-disable; 5xx/429/timeout transient | Major | FR-27, FR-38 |
| SC-DEL-020 | 410 Gone auto-disable | Endpoint returns 410 | Auto-disable subscription, alert owner; stop retrying | Major | FR-27, FR-37 |
| SC-DEL-021 | 429 Retry-After honored | Receiver rate-limits with Retry-After | Schedule next attempt per Retry-After (capped) | Major | FR-27, NFR-7 |
| SC-DEL-022 | Non-2xx after successful processing | Receiver did work then errored on response | Gateway retries; rely on receiver idempotency | Minor | NFR-4, FR-26 |
| SC-DEL-023 | Retry after partial/chunked write | Conn dropped mid response body | Treat as failure, retry; idempotency handles dupes | Major | FR-27, NFR-4 |
| SC-DEL-024 | Exhausted retries → DLQ, no silent drop | Final attempt fails | Persist to dead_letters w/ context; alert; Admin UI | Critical | FR-27, FR-36, FR-38 |
| SC-DEL-025 | DLQ accumulation w/o monitoring | DLQ depth grows unnoticed | Dashboard + alert on DLQ depth; metric emitted | Major | FR-39, FR-38 |
| SC-DEL-026 | Manual / bulk DLQ replay | Operator replays dead letters | Rate-limited replay; preserves original delivery_id | Major | FR-27, FR-38 |
| SC-DEL-027 | DLQ retention expiry (RPO) | Dead letters age out | Configurable retention; document RPO; expire+metric | Minor | NFR-6, FR-36 |
| SC-DEL-028 | Poison / unprocessable event | Event can never succeed | Short-circuit to DLQ without burning full retry budget | Major | FR-27, FR-36 |
| SC-DEL-029 | Replay re-introduces duplicates | DLQ/point-in-time replay | Preserve original idempotency key so receiver dedupes | Major | FR-26, NFR-4, NFR-6 |
| SC-DEL-030 | No global ordering guarantee | Cross-topic / cross-sub events | Document no global order; order-independent consumers | Major | NFR-3, R-11 |
| SC-DEL-031 | Out-of-order arrival within sub | Retry/reorder delivers stale after newer | Receiver uses version/seq+timestamp; discard stale | Major | NFR-3 |
| SC-DEL-032 | Head-of-line blocking | One subject's slow delivery stalls others | Isolate per subject; slow subject MUST NOT block others | Major | FR-25, NFR-7 |
| SC-DEL-033 | Per-subject keyed ordering | Multiple events same subject | Per-sub per-subject sequence; ordered within key | Major | NFR-3, FR-25 |
| SC-DEL-034 | Cross-topic ordering assumed by consumer | Consumer expects A-before-B across topics | Never guaranteed; documented; consumer must not rely | Minor | NFR-3, R-11 |
| SC-DEL-035 | Reordering after outage | Backlog drains out of order post-recovery | State-based validation (version compare), not arrival order | Major | NFR-3, NFR-6 |
| SC-DEL-036 | Connection refused / DNS failure | Endpoint host down / unresolvable | Treat transient; retry w/ backoff; count toward breaker | Major | FR-27, FR-38 |
| SC-DEL-037 | TLS handshake / cert errors | Invalid/expired/untrusted cert | Fail closed (no insecure fallback); retry transiently; alert | Major | FR-34, NFR-8, R-3 |
| SC-DEL-038 | Read/connect timeout | Receiver hangs | Send-side timeout (~15s); abort, retry | Major | NFR-2, FR-27 |
| SC-DEL-039 | Slowloris trickle response | Receiver dribbles bytes to hold connection | Hard total + idle timeout; cap response bytes; abort | Major | NFR-2, NFR-7 |
| SC-DEL-040 | Very slow but eventually-200 | Receiver always near timeout | Encourage 202+async on receiver; enforce timeout | Minor | NFR-2 |
| SC-DEL-041 | Redirect-following SSRF | Receiver 3xx to private/internal address | Do not follow redirects to private ranges; block SSRF | Critical | R-3, FR-34 |
| SC-DEL-042 | Zombie endpoint clogging | Endpoint perpetually failing | Circuit breaker opens after N failures; isolate | Major | NFR-7, FR-38 |
| SC-DEL-043 | Circuit-breaker half-open probe | Cooldown elapsed | Single probe; close on success, re-open on failure | Major | FR-38, NFR-7 |
| SC-DEL-044 | Subscriber 2xx but body says error | Status 200, body `{"error":...}` | HTTP status is source of truth → treated success | Minor | FR-28 |
| SC-DEL-045 | Consumer lag building | L2 pending/XLEN climbing | Monitor depth; alert ~70% capacity; metric emitted | Major | FR-39, NFR-1 |
| SC-DEL-046 | Backpressure signaling | System overloaded | 429+Retry-After where applicable; queue not drop | Major | NFR-1, FR-39 |
| SC-DEL-047 | Retry storm amplifies overload | Failures generate retries worsening load | Jitter + retry budget + circuit breaker cap rate | Major | NFR-1, NFR-7 |
| SC-DEL-048 | Large fan-out / high cardinality | One event matches huge number of subs | Queue-first, per-sub isolation; bounded concurrency | Major | FR-25, NFR-1, FR-21 |
| SC-DEL-049 | Graceful degradation under load | Sustained burst | Shed/queue (backpressure) not drop; ingress 202-fast | Major | NFR-1, NFR-7 |
| SC-DEL-050 | XADD then crash before XREADGROUP | Gateway crashes after persist+XADD | Entry durable in stream; new consumer picks up; no loss | Critical | FR-24, NFR-5, R-4 |
| SC-DEL-051 | Consumer crash mid-process | Worker dies after XREADGROUP before XACK | Entry stays in PEL; recovered via XAUTOCLAIM | Critical | FR-24, NFR-5, R-4 |
| SC-DEL-052 | Stalled message reassignment | Message idle > min-idle-time | XCLAIM/XAUTOCLAIM reassigns to live consumer | Major | NFR-5, FR-24 |
| SC-DEL-053 | Double-claim race | Two workers race to claim same entry | min-idle-time reset ensures single ownership | Major | NFR-5, FR-24 |
| SC-DEL-054 | Claim of trimmed/deleted entry | XAUTOCLAIM hits trimmed id | Skips and purges dangling PEL entry; no crash | Minor | FR-24, NFR-6 |
| SC-DEL-055 | Missing XACK → infinite redelivery | Code forgets XACK after commit | Always XACK after commit; monitor PEL depth/age | Critical | FR-24, FR-39, NFR-5 |
| SC-DEL-056 | Unbounded stream memory | Stream grows without trim | XADD MAXLEN/MINID trim; bounded memory | Major | FR-24, NFR-6 |
| SC-DEL-057 | Trimming un-consumed entries = loss | Trim before slowest consumer caught up | Size MAXLEN/retention to slowest consumer lag; alert | Critical | NFR-6, FR-24 |
| SC-DEL-058 | Approximate ~MAXLEN overshoot | `XADD MAXLEN ~` keeps extra entries | Accept overshoot; capacity-plan for it | Minor | FR-24, NFR-6 |
| SC-DEL-059 | Idle/abandoned consumer accumulation | Dead consumers linger in group | Reap idle consumers; reclaim their PEL first | Minor | NFR-5, FR-24 |
| SC-DEL-060 | Redis down / failover | Primary fails, replica promoted | AOF/persistence on; un-replicated tail may be lost → bounded RPO | Critical | NFR-6, R-4, NFR-5 |
| SC-DEL-061 | No double-delivery across instances | Multiple gateway workers in same group | Consumer group → each entry to exactly one consumer | Critical | NFR-5, FR-24 |
| SC-DEL-062 | Balanced consumption across workers | N workers in group | Stream distributes entries; roughly balanced | Minor | NFR-5, NFR-1 |
| SC-DEL-063 | Worker scale-down loses in-flight | Worker removed with PEL entries | Survivors XAUTOCLAIM the dead worker's PEL; no loss | Major | NFR-5, FR-24 |
| SC-DEL-064 | Independent consumer groups | SSE/WS group vs HTTP-callback group | Separate groups consume stream independently | Minor | FR-24, R-8 |
| SC-DEL-065 | Hot-key / partition skew | One subject-key dominates | Document skew; per-sub queue isolates; consider sharding | Minor | NFR-3, NFR-1 |
| SC-DEL-066 | Point-in-time replay | Operator replays from stream id/time | Replay from stream; preserve idempotency keys; rate-limit | Major | NFR-6, FR-26 |
| SC-DEL-067 | Message TTL / expiry (RPO) | Events older than retention | Expire per retention; document RPO; near-expiry metric | Minor | NFR-6 |
| SC-DEL-068 | Clock skew | Worker/Redis/provider clocks drift | NTP-sync; rely on Redis stream id for ordering | Minor | NFR-3 |
| SC-DEL-069 | Replay-attack window (egress) | Old signed delivery replayed to receiver | Signed timestamp + nonce + tolerance in delivery envelope | Major | FR-26, FR-34, NFR-8 |
| SC-DEL-070 | Audit trail of every attempt | Compliance / debugging | Persist every attempt (delivery_attempts) w/ redacted secrets | Major | FR-38, FR-36, NFR-6 |
| SC-DEL-071 | Partial outage isolation | One region/endpoint group down | Failures isolated per-sub; healthy subs unaffected | Major | NFR-7, FR-25 |
| SC-DEL-072 | Oversized / decompression-bomb payload | Receiver returns huge body / event huge | Cap decompressed size per codec; abort; reject oversize | Major | NFR-2, R-3 |
| SC-DEL-073 | Deep / abusive JSON | Pathologically nested envelope | Limit nesting depth on parse; reject; no stack blowup | Minor | NFR-2, R-3 |
| SC-DEL-074 | Thin-vs-fat payload tradeoff | Deciding delivery payload size | Document thin (id+re-fetch) vs fat tradeoff; prefer absolute | Minor | FR-28, NFR-3 |
| SC-DEL-075 | Effectively-once via idempotent receiver | at-least-once + receiver dedup | End-to-end effect is once; documented target contract | Major | NFR-4, FR-26, R-11 |
| SC-DEL-076 | SSE/WS best-effort vs HTTP durable | Live stream subscriber drops | SSE/WS best-effort (no DLQ); HTTP-callback durable+DLQ; document | Major | R-8, FR-30, FR-27 |
| SC-DEL-077 | Ingress stays 202-fast under delivery load | Heavy delivery backlog | Ingress verify→202 unaffected (decoupled via L2) | Critical | NFR-1, FR-12, FR-24 |

---

## 3. SEC — Security & abuse

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-SEC-001 | Missing/optional inbound signature verification | `recipe=none` or toggle off in prod | Reject unverified (400/401); `none` disallowed in prod; never optional for external | Critical | FR-8, FR-33, R-2 |
| SC-SEC-002 | Non-constant-time signature compare | Attacker probes HMAC via timing | Constant-time compare (`hmac.compare_digest`); no early-exit | High | FR-8, R-2 |
| SC-SEC-003 | Algorithm confusion / downgrade | Payload/header claims weaker/`none` alg | Hardcode HMAC-SHA256 per descriptor; never trust payload `alg` (CWE-347) | Critical | FR-8, R-2 |
| SC-SEC-004 | HMAC key confusion across providers/tenants | Secret for A validates payload routed as B | Scope secret per connector; select by route/conn-id not payload | Critical | FR-8, FR-33, FR-35 |
| SC-SEC-005 | Payload canonicalization / byte-drift | Framework re-serializes body before verify | Verify over exact raw request bytes captured pre-parse | High | FR-8, FR-11, R-2 |
| SC-SEC-006 | Signed-metadata gap | Sig covers body only; id/ts forgeable | Sign over `id`+`timestamp`+`body` (Standard Webhooks) | High | FR-8, FR-9, R-2 |
| SC-SEC-007 | Unauthenticated ingress | Accepts arbitrary unsigned POST | Require per-connector secret/IP allowlist; opaque conn-id alone insufficient | High | FR-7, FR-33 |
| SC-SEC-008 | Side-effects before verification | Event persisted/dispatched/echoed pre-verify | Verify first, return early; no write/dispatch pre-verify | Critical | FR-8, FR-12, R-9 |
| SC-SEC-009 | Handshake-before-verify conn-id oracle | Slack challenge echoed before verify | Answer handshake only after verified+attributed (CWE-203) | High | FR-10, R-9 |
| SC-SEC-010 | Existence/state leakage pre-verification | Differential response/timing reveals conn-id | Uniform error responses + timing for unknown/invalid (CWE-203) | High | FR-33, R-9 |
| SC-SEC-011 | Replay of captured valid request | Attacker resends valid signed POST | Reject outside signed-ts tolerance (~5min); dedup on message-id | High | FR-9, FR-23 |
| SC-SEC-012 | Replay tolerance misconfigured | Tolerance 0 or hours | Enforce sane bounds; never 0/hours; sensible default | Medium | FR-9 |
| SC-SEC-013 | Missing idempotency / duplicate processing | Provider retransmits / duplicate | Dedup on `(source, event.id)` per tenant; idempotent processing | High | FR-23, FR-26, NFR-4 |
| SC-SEC-014 | Timestamp/nonce store bypass cross-tenant | Replay store keyed without tenant scope | Scope dedup/nonce store per `team_id`+connector | High | FR-23, FR-35 |
| SC-SEC-015 | Plaintext secrets at rest | Secret/credentials stored unencrypted | Encrypt at rest (KMS); `webhook_signing_secret` encrypted | Critical | NFR-8, FR-3 |
| SC-SEC-016 | Secrets in code/VCS/dumps | Secret committed / in dump / bundle | Secrets manager; scan; redact in dumps; rotate on exposure | High | NFR-8, FR-36 |
| SC-SEC-017 | No rotation / rotation downtime | Single secret; rotation drops in-flight | Dual-secret overlap window; accept old+new during cutover | Medium | NFR-8, FR-3 |
| SC-SEC-018 | Compromised secret not revocable | Leaked secret can't be invalidated | Immediate expire/revoke; last-used audit; re-provision | High | NFR-8 |
| SC-SEC-019 | Cross-tenant shared secret | Same secret reused across tenants | Per-tenant per-connector secrets; deny shared | High | NFR-8, FR-35 |
| SC-SEC-020 | SSRF to private/loopback/link-local on callback | callback resolves to RFC1918/127/::1/169.254/multicast | Resolve and reject private ranges at create AND delivery | Critical | R-3, FR-14, FR-28 |
| SC-SEC-021 | Cloud metadata exfil via callback | callback→169.254.169.254 / metadata host / IMDS | Deny metadata IPs/hostnames; egress proxy; isolated subnet | Critical | R-3, FR-28 |
| SC-SEC-022 | SSRF blocklist bypass via encoding | decimal/octal/hex/IPv6-mapped/`0`/`@`-userinfo | Canonical IP parse before check; prefer allowlist | High | R-3 |
| SC-SEC-023 | DNS rebinding / TOCTOU on egress | DNS public at validate, private at connect | Resolve once, pin IP, connect to pinned IP, validate at connect | Critical | R-3, FR-28 |
| SC-SEC-024 | Redirect-following to internal | Callback 3xx to internal/metadata URL | Disable auto-redirects (or re-validate+pin each hop) | High | R-3, FR-28 |
| SC-SEC-025 | Non-HTTPS / dangerous scheme egress | callback=`file://`/`gopher://`/`ftp://`/`dict://`/`http://` | https-only allowlist; reject other schemes | High | R-3, FR-14, FR-34 |
| SC-SEC-026 | Unexpected port / internal service | callback to non-443 internal port | Restrict to 443 (configurable allowlist); egress firewall | Medium | R-3, FR-28 |
| SC-SEC-027 | SSRF via response reflection | Upstream callback body/headers reflected to subscriber/UI | Don't reflect upstream response; log status/size only | Medium | R-3, FR-36 |
| SC-SEC-028 | Cross-tenant subscription BOLA/IDOR | Caller reads/edits another tenant's sub by id | Enforce `subscription.team_id == caller`; object-level authz | Critical | FR-35, FR-16 |
| SC-SEC-029 | Cross-tenant event delivery | Event for A matched to B's sub | Tag event tenant at ingest; tenant-leading index; assert on worker | Critical | FR-19, FR-23, FR-35 |
| SC-SEC-030 | Subscription CRUD authz gap | Create/delete without role/object checks | Object-level authz + role checks on all CRUD | High | FR-13, FR-16, FR-37 |
| SC-SEC-031 | Confused-deputy subscription target | Sub targets event-types tenant isn't entitled to | Authorize requested event-types vs tenant entitlements | High | FR-4, FR-17, FR-35 |
| SC-SEC-032 | Shared cache/pool context bleed | L1 cache/session pool leaks across tenants | Tenant id in cache keys; reset session per use | High | NFR-5, FR-35 |
| SC-SEC-033 | Cross-tenant secret/connector reuse | Sub references another tenant's connector/secret | Tenant-scoped connectors; deny cross-tenant reference | High | FR-35, NFR-8 |
| SC-SEC-034 | Guessable connector/subscription id | Sequential/low-entropy conn-id | Long opaque high-entropy id + auth; not sole control | High | FR-7, FR-33 |
| SC-SEC-035 | Capability URL leaks via Referer/logs | conn-id in query → Referer / access logs | Ids in headers/path not query; `Referrer-Policy`; redact logs (CWE-598) | High | FR-7, FR-36, FR-33 |
| SC-SEC-036 | Secret/ID in query string | Signing material as `?secret=` | Never in query; redact in logs | High | FR-36, FR-33 |
| SC-SEC-037 | Capability URL never rotates | Leaked conn-id permanent | Support regen/revoke of conn-id | Medium | FR-7, FR-33 |
| SC-SEC-038 | Log injection / CRLF forging | Payload/headers contain CR/LF to forge logs | Neutralize CR/LF; structured logging (CWE-117) | Medium | FR-36, FR-39 |
| SC-SEC-039 | Credential leakage in logs | Secrets/Authorization written to logs/DLQ/event_log | Redact secrets + Authorization everywhere | High | FR-36, NFR-8 |
| SC-SEC-040 | PII in payloads unredacted | Event payloads with PII persisted | Redact/minimize; retention; encrypt at rest | High | FR-36, NFR-6, NFR-8 |
| SC-SEC-041 | Downstream injection via event data | Untrusted field renders in Admin UI (stored XSS) | Treat event data untrusted; encode at sinks; escape in UI | High | FR-37, FR-40 |
| SC-SEC-042 | Error / stack-trace disclosure | Verification/parse error returns internals | Generic error responses; no stack traces to caller | Medium | FR-33 |
| SC-SEC-043 | Oversized payload DoS | Huge inbound body buffered into memory | Hard max body; `413` before buffering (CWE-400/770) | High | NFR-1, NFR-7, FR-12 |
| SC-SEC-044 | Decompression bomb | Small compressed body expands huge (gzip/br/zstd/snappy) | Cap decompressed size per codec; abort (CWE-409) | High | NFR-1, NFR-7 |
| SC-SEC-045 | Header injection / request smuggling | Malicious/duplicate/CRLF headers on ingress | Strip CRLF; header allowlist; reject smuggling | Medium | FR-33 |
| SC-SEC-046 | Ingress flood / amplification | High-rate POSTs per source/connector/tenant | Rate-limit per source/connector/tenant; reject unverified early; cap fan-out | High | NFR-1, NFR-7, FR-21 |
| SC-SEC-047 | Outbound queue exhaustion via slow/dead endpoints | Subscriber hangs/dead, queue fills | Per-tenant quotas; timeout; circuit-breaker; per-sub queue isolation | High | FR-25, NFR-7, FR-27 |
| SC-SEC-048 | Retry storm / unbounded retries | Failing target triggers infinite retries | Bounded jittered backoff + DLQ | High | FR-27, NFR-4, R-11 |
| SC-SEC-049 | Billing / quota abuse | Tenant floods to inflate usage/cost | Per-tenant quotas + anomaly alerts | Medium | NFR-1, FR-39 |
| SC-SEC-050 | Plaintext / weak TLS on egress | Egress over http or TLS<1.2, no chain validation | https + full chain validation; TLS 1.2+ | High | FR-34, FR-28 |
| SC-SEC-051 | Outbound callback signing absent | Subscriber can't verify gateway-originated POST | Sign every callback (HMAC + ts + id); Idempotency-Key present | High | FR-28, FR-26, FR-34 |
| SC-SEC-052 | mTLS / outbound auth not supported | High-security subscriber needs mTLS/OAuth2 | Offer mTLS / OAuth2 bearer w/ expiry per `delivery.auth.strategy` | Medium | FR-34 |
| SC-SEC-053 | Insufficient audit logging | No record of CRUD/secret-rotation/auth/delivery | Tamper-evident audit, tenant-scoped | High | FR-38, FR-39, NFR-8 |
| SC-SEC-054 | CSRF on cookie-session admin mgmt routes | Admin UI sub CRUD via cookie session | CSRF tokens / SameSite (N/A for token/HMAC APIs) | Medium | FR-37 |
| SC-SEC-055 | SSRF validation skipped on update | URL validated at create but not on update/redelivery | Re-validate on create AND update AND at delivery (pin-and-connect) | Critical | R-3, FR-28 |

---

## 4. SUB — Subscription lifecycle, routing/CEL & fan-out

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-SUB-001 | Create subscription | `POST /subscriptions` valid (filter→target) | Server-assigns id, validates, persists, indexes, 201 | Critical | FR-13, FR-16 |
| SC-SUB-002 | Retrieve subscription | GET known id | Return record (filter, mode, target, active, owner) | Major | FR-13, FR-37 |
| SC-SUB-003 | Retrieve unknown subscription | GET unknown/deleted id | 404, no leak of other-tenant ids | Major | FR-13 |
| SC-SUB-004 | List/query subscriptions | List call | Paginated, tenant/team-scoped | Major | FR-35, FR-37 |
| SC-SUB-005 | Update subscription — filter immutability | Update changes CEL filter | Document mutable vs immutable: 409 recreate OR atomic cut-over | Major | FR-13, —, propose FR |
| SC-SUB-006 | Update subscription — target/active | Toggle active or change sink | Atomic update, refcount/hook policy honored | Major | FR-13, FR-37 |
| SC-SUB-007 | Delete subscription drains in-flight | DELETE with deliveries queued | Cancel/drain per-sub queue, decrement refcount, deregister if last | Critical | FR-13, FR-17, FR-25 |
| SC-SUB-008 | Delete idempotent | DELETE already-deleted id | 200/204, no error, no double-decrement | Major | FR-13, —, propose FR |
| SC-SUB-009 | Idempotent re-create | Re-POST identical (sink+filter+tenant) | Return existing (200), no second hook registration | Major | FR-15, FR-17 |
| SC-SUB-010 | Duplicate distinct subscriptions | Two POSTs same sink+filter, no idem key | Allow distinct ids; dedup by hash OR consumer idempotency; document | Minor | FR-26, —, propose FR |
| SC-SUB-011 | Bulk subscription ops partial failure | Batch create, one invalid | Partial-failure report; NO half-registered hooks | Major | FR-16, FR-17, —, propose FR |
| SC-SUB-012 | Concurrent subscribe/unsubscribe race | Parallel sub+unsub same connector | Lock/optimistic concurrency; no lost-update, no orphan hook | Critical | FR-17, R-5 |
| SC-SUB-013 | Per-tenant subscription quota | Create beyond tenant limit | Reject 429; no partial registration | Major | FR-35, —, propose FR |
| SC-SUB-014 | Paused/disabled subscription | `active=false` but event matches | Matched but NOT delivered; document refcount policy | Major | FR-37, —, propose FR |
| SC-SUB-015 | Orphaned subscription, target gone | Callback persistently unreachable | Mark unhealthy, auto-disable at threshold, alert/meter | Major | FR-38, R-8 |
| SC-SUB-016 | Subscription pinned to payload version | Sub declares dataschema/version | Match only pinned version (or document default=all) | Major | FR-11, —, propose FR |
| SC-SUB-017 | Additive payload field | Provider adds optional field | Backward compatible; `has()`-guarded filters still match | Minor | FR-18, —, propose FR |
| SC-SUB-018 | Breaking/renamed payload field | Field renamed/removed upstream | CEL `no_such_field` absorbed as no-match; no crash | Major | FR-18 |
| SC-SUB-019 | Filter update while events in flight | Mutable update mid-stream | Defined cut-over; one consistent filter version; no half-applied | Major | FR-18, —, propose FR |
| SC-SUB-020 | Fan-out to N subscribers | 1 event matches N subs | N independent per-sub queues; per-sub isolation | Critical | FR-19, FR-21, FR-25, NFR-7 |
| SC-SUB-021 | Register upstream hook on first sub | First sub for connector event | Single upstream registration (auto-register via OAuth) | Critical | FR-17, R-5 |
| SC-SUB-022 | Increment refcount on subsequent sub | 2nd+ sub same connector source | Increment refcount, NO re-registration | Major | FR-17, R-5 |
| SC-SUB-023 | Deregister hook on last unsub | Last sub removed | Decrement to 0, deregister upstream webhook | Critical | FR-17, R-5 |
| SC-SUB-024 | Non-last unsub keeps hook | Remove 1 of N subs | Hook retained, refcount=N-1, NO deregister | Critical | FR-17, R-5 |
| SC-SUB-025 | Upstream hook registration fails | Provider API error on auto-register | Fail atomically/roll back OR mark pending; never false success | Critical | FR-17, R-5 |
| SC-SUB-026 | Insufficient OAuth scope at subscribe | Missing e.g. `admin:repo_hook` | Detect at subscribe, actionable error, no dangling refcount | Major | FR-4, FR-17 |
| SC-SUB-027 | Auto-register vs manual hook capability | Provider lacks auto-register API | Detect capability; manual hook fallback; document | Minor | FR-5, FR-17 |
| SC-SUB-028 | Subscribe to non-emitted event type | Sub for type connector never emits | Reject at create (validate vs taxonomy) OR accept idle+warn | Minor | FR-11, FR-20, —, propose FR |
| SC-SUB-029 | Idle subscription | Valid sub, no matching events ever | No error, no leak, zero-deliveries metric | Minor | FR-39 |
| SC-SUB-030 | Event matches zero subscriptions | Accepted event, no candidate subs | Drop + debug log/meter; optional unmatched archive | Minor | FR-19, FR-39, —, propose FR |
| SC-SUB-031 | Refcount drift/leak | Live subs ≠ registered hooks | Reconciliation loop detects+repairs drift, meters leak | Major | R-5, —, propose FR |
| SC-SUB-032 | CEL valid match | Filter evaluates true | Deliver to target | Critical | FR-18 |
| SC-SUB-033 | CEL no match | Filter evaluates false | Do NOT deliver | Critical | FR-18 |
| SC-SUB-034 | Malformed CEL at create | Syntactically invalid expr | Reject at create (validating admission), 422 | Critical | FR-16, FR-18, —, propose FR |
| SC-SUB-035 | Never-validated expr fails at runtime | Expr errors at eval | Catch, fail-closed (no-match), meter; never crash loop | Critical | FR-18, FR-19 |
| SC-SUB-036 | CEL references missing field | Expr uses absent field | `has()` guard; `&&`/`||` absorb errors; no-match not crash | Major | FR-18 |
| SC-SUB-037 | CEL evaluation exception | `no_matching_overload`/runtime err | No-delivery fail-closed + log/meter | Major | FR-18 |
| SC-SUB-038 | CEL cost/timeout limit | Expensive/unbounded expr | Static cost estimate at create + runtime budget; halt over-budget | Major | FR-18, —, propose FR |
| SC-SUB-039 | CEL type mismatch | int vs string compare | Compile-time rejection preferred (at create) | Major | FR-18, —, propose FR |
| SC-SUB-040 | Null vs absent data field | present-null vs absent | Distinguish via `has()`; skip null paths; no crash | Major | FR-18 |
| SC-SUB-041 | Deeply nested data access | `data.a.b.c.d` | Presence-guard each level; cap depth; over-depth rejected at create | Minor | FR-18, —, propose FR |
| SC-SUB-042 | Short-circuit/error-absorption | `false&&bad`, `true||bad`, ternary, `exists` | Per CEL semantics; error absorbed where short-circuited | Major | FR-18 |
| SC-SUB-043 | Glob/prefix event-type match | `com.stripe.*` vs full type | Anchored prefix; case-sensitivity defined; ReDoS-safe | Major | FR-20 |
| SC-SUB-044 | Tautology filter (catch-all) | Filter always true | Allowed but cost-bound; fan-out scale considered | Minor | FR-18, FR-20 |
| SC-SUB-045 | Contradiction filter | Filter always false | Warn at create (dead subscription) | Minor | FR-18, —, propose FR |
| SC-SUB-046 | Attributes-only vs payload filtering | Filter scopes envelope attrs vs `data` | Document scope; fast-path type/attr equality before full CEL | Major | FR-18 |
| SC-SUB-047 | Callback URL validation at create | Sub with unreachable/invalid callback | Handshake (WebSub/Event Grid/CloudEvents); reject if handshake fails | Critical | FR-10, FR-14, FR-28, R-3 |
| SC-SUB-048 | SSRF via callback URL | Caller-supplied callback to internal addr | SSRF guard (block private/link-local/metadata) before registration | Critical | FR-34, R-3 |
| SC-SUB-049 | Delivery retries with backoff | Transient callback failure | Retry with exponential backoff per-sub; isolation maintained | Major | FR-25, FR-27, FR-38 |
| SC-SUB-050 | Permanent failure → DLQ | NO_PERMISSIONS/NO_RESOURCE/CONNECTION_FAILURE | Route to dead_letters without futile retry; meter | Major | FR-27, FR-36 |
| SC-SUB-051 | DLQ delivery itself fails | DLQ write/forward fails | Drop + log + alert; document "DLQ-of-DLQ" gap | Minor | FR-36, —, propose FR |
| SC-SUB-052 | At-least-once duplicate delivery | Retry after ambiguous success | Carry `Idempotency-Key=delivery_id`; consumer dedupes | Major | FR-26, NFR-4 |
| SC-SUB-053 | Out-of-order delivery | No global order across subjects | Per-subject ordering only (per-sub queue + stream id); document | Major | FR-25, NFR-3, R-11 |
| SC-SUB-054 | Reply leak / mis-routing | Correlate-mode reply re-emitted | Re-filter replies; do NOT blindly re-emit (avoid loop/leak) | Major | FR-22, FR-31 |
| SC-SUB-055 | Correlate exact-match routing | Event w/ correlation_value | Route by EXACT match; no fan-out; resume waiting run | Critical | FR-22 |
| SC-SUB-056 | Correlate no waiting run | correlation_value matches nothing | No resume; defined drop/park + meter, no crash | Major | FR-22, —, propose FR |
| SC-SUB-057 | Fan-out does not require waiting agent | spawn-mode event | New run per matching sub; never blocks on a waiter | Major | FR-21 |

---

## 5. COR — Async tool / correlate mode (Tasks + #523)

> The 2026-07-28 RC gives the Task lifecycle (`tasks/get`/`update`/`cancel`, server-directed creation, no `tasks/list`) but **no native completion webhook or correlation** — correlate mode is gateway-owned (per `#523`). Identifiers here are **provisional**; tests must use a tolerant parser.

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-COR-001 | Normal task completion resumes exact run | tools/call→task+202; working→completed via poll or notifications/tasks/status | fetch result, match taskId→pending correlation_value, resume EXACT run, mark done | Critical | FR-22, FR-29, FR-44 |
| SC-COR-002 | Task fails | task →failed | resume SAME waiting run with failure; never spawn new | Critical | FR-22, FR-29 |
| SC-COR-003 | Task cancelled by gateway | we issue tasks/cancel → cancelled | resume run with cancellation, stop polling | Major | FR-22, FR-29 |
| SC-COR-004 | Task cancelled upstream/external | external cancel discovered on poll | resume run as cancelled; stop polling | Major | FR-22, FR-29 |
| SC-COR-005 | Cancel rejected — already terminal | tasks/cancel → -32602 | swallow error, read actual terminal result, resume | Minor | FR-22, FR-29 |
| SC-COR-006 | Task never completes (timeout) | TTL elapses / watchdog, no terminal | synthesize timeout result, resume timed-out, stop polling, alert; never hang | Critical | FR-22, NFR-2, R-1 |
| SC-COR-007 | Completion after subscription/run expired | correlate sub TTL purged; no live waiter | do NOT spawn; drop→dead-letter+audit; optionally persist for late pull | Major | FR-22, FR-36, R-8 |
| SC-COR-008 | Receiver purged expired task | tasks/get/result → -32602 "expired" | resolve run as expired (distinct from never-existed); alert if should've completed | Major | FR-22, R-1 |
| SC-COR-009 | Duplicate completion | notification redelivery / webhook retry / poll race | idempotent on taskId: first resumes+consumes, rest no-op | Critical | FR-23, FR-26, NFR-4 |
| SC-COR-010 | Unknown/wrong correlation id | completion has no matching pending-run | reject unmatched; never auto-create; log+dead-letter; verify #523 sig first | Critical | FR-22, FR-33, R-8 |
| SC-COR-011 | Correlation-id collision | two calls map to same key | gateway mints globally-unique correlation_value; detect collision, fail-closed | Critical | FR-22, R-1 |
| SC-COR-012 | Guessable/cross-context task id | completion bears foreign-context taskId | enforce auth-context binding (team/owner); reject foreign | Critical | FR-33, FR-35, R-3 |
| SC-COR-013 | Waiting run already finished/crashed | completion arrives; run record gone | no respawn; idempotent deliver if resolvable else dead-letter; audit | Major | FR-22, FR-36, R-8 |
| SC-COR-014 | Out-of-order task updates | stale working after completed | use lastUpdatedAt + terminal monotonicity; ignore regressions | Major | NFR-3, FR-44 |
| SC-COR-015 | Illegal state regression | terminal→non-terminal reported | terminal is sticky; reject regression, log violation | Major | NFR-3, FR-44 |
| SC-COR-016 | Progress / partial updates | notifications/progress, interim working | forward as progress; do NOT resume/consume correlation | Minor | FR-44, FR-30 |
| SC-COR-017 | input_required mid-task elicitation | task → input_required | surface to agent/user; InputRequiredResult+state token; keep correlation alive | Major | FR-22, FR-44, R-1 |
| SC-COR-018 | Status notification never sent | notifications/tasks/status optional/absent | always run tasks/get poll loop (pollInterval) as source of truth | Major | FR-43, R-1 |
| SC-COR-019 | #523 webhook completion unreachable | gateway's webhook target down/unregistered | fall back to polling; downstream agent callback queue+retry+dead-letter | Major | FR-27, FR-31, R-8 |
| SC-COR-020 | #523 webhook signature/auth failure | inbound completion fails validation | reject 401/403, resume nothing, log; never trust forged completion | Critical | FR-8, FR-33, FR-42 |
| SC-COR-021 | Correlate subscription auto-expiry (TTL) | sub entry hits TTL before completion | expire deterministically; resolve run timed-out; reject later completion | Major | FR-22, NFR-6, R-8 |
| SC-COR-022 | Poll storm / receiver rate-limit | many pending tasks / 429 | honor pollInterval, jittered backoff, cap concurrent polls, coalesce per taskId | Major | NFR-1, NFR-7 |
| SC-COR-023 | tasks/get transient errors | -32603 / network blip | retry with backoff, keep pending; resolve only on terminal or run timeout | Major | NFR-1, R-11 |
| SC-COR-024 | Server-directed task surprise (RC) | server turns normal tools/call into task | detect CreateTaskResult shape on ANY call; open correlation; switch async | Major | FR-43, FR-44, R-1 |
| SC-COR-025 | No tasks/list to reconcile (RC) | RC removed tasks/list; restart loses map | persist pending-run↔taskId map durably; on restart re-poll known ids | Critical | NFR-5, NFR-6, R-1 |
| SC-COR-026 | Result fetched but run never resumed | resume step crashes mid-flight | persist fetched result; resume retryable/idempotent; don't re-fetch purged | Major | FR-26, NFR-4 |
| SC-COR-027 | Duplicate tasks/result race | notification + poll both reach terminal | single-flight per taskId; exactly one resume | Critical | FR-23, FR-26, NFR-4 |

---

## 6. MCP — MCP-native notifications

> Today the gateway tears down each upstream `ClientSession` after `list_*` and **drops** notifications (`gateway_service.py`). The persistent-session + synthesized-id work is **milestone M6** (risk **R-6**). MCP notifications are uri-only, at-least-once, per-stream-ordered → the gateway **must synthesize a stable dedup id** and **resubscribe on every new session**.

| ID | Scenario | Trigger / condition | Expected handling | Severity | FRD refs |
|----|----------|---------------------|-------------------|----------|----------|
| SC-MCP-001 | Normal resource-updated | `notifications/resources/updated {uri}` on subscribed uri | Hold session; synthesize id; normalize (`type=…resources.updated`, `subject=uri`); `resources/read` if content needed; XADD | Critical | FR-32, FR-31, FR-11, FR-44, R-6 |
| SC-MCP-002 | Notification has no provider id | Payload uri (+optional title) only | Synthesize id=hash(serverId+uri+seq/timestamp) or content hash; use as dedup key; never null id | Critical | FR-23, R-10, FR-32 |
| SC-MCP-003 | Duplicate / redelivered notification | At-least-once redelivery (Last-Event-ID replay) | Dedup on synthesized id within TTL; collapse fan-out; no double-trigger | Major | FR-23, NFR-4, R-10, R-11 |
| SC-MCP-004 | Session drops → reconnect → resubscribe | Upstream 404 on `Mcp-Session-Id` | Re-initialize, re-list, RE-ISSUE all `resources/subscribe`; reconcile | Critical | FR-32, R-6, FR-17, NFR-5 |
| SC-MCP-005 | Reconnect via Last-Event-ID per stream | Stream breaks but session valid | Resume same stream only; no cross-stream replay; still dedup; full resubscribe only on session 404 | Major | FR-43, R-6, NFR-3, R-11 |
| SC-MCP-006 | Missed updates during downtime gap | Resource changed while disconnected; no replay | After resubscribe, proactively `resources/read` + re-list; emit synthetic "may-have-missed" | Major | FR-32, NFR-6, R-6, —, propose FR |
| SC-MCP-007 | Server doesn't support subscriptions | `resources.subscribe` absent/false | Don't call subscribe; poll fallback or listChanged; surface "no live updates" | Major | FR-32, FR-43, —, propose FR |
| SC-MCP-008 | subscribe returns error / unsupported method | Advertised but rejected / method-not-found | Degrade to polling; record capability mismatch; don't crash | Major | FR-32, R-6 |
| SC-MCP-009 | Subscribe to nonexistent resource | `resources/subscribe` → -32002 | Surface error; no phantom subscription/refcount | Minor | FR-32, FR-17 |
| SC-MCP-010 | tools/list_changed invalidates cache | `notifications/tools/list_changed` | Invalidate cache; re-fetch `tools/list`; re-expose; ensure reconnect re-queries tools | Major | FR-44, R-6, FR-32 |
| SC-MCP-011 | resources/list_changed invalidates cache | `notifications/resources/list_changed` | Re-fetch `resources/list`; reconcile subs (drop vanished uris) | Major | FR-44, FR-32, R-6 |
| SC-MCP-012 | Resource removed while subscribed | uri gone (via list_changed) | Emit "resource gone"; tear down sub+refcount; stop `resources/read` | Major | FR-44, FR-32, R-5 |
| SC-MCP-013 | Notification with no interested subscriber | refcount 0 for uri | Don't subscribe upstream when 0; or accept-and-drop; `resources/unsubscribe` when last leaves | Minor | FR-32, R-5 |
| SC-MCP-014 | High-frequency notifications debounce/coalesce | Rapid repeated updated for same uri | Debounce per-uri window; refetch once after quiescence; rate-limit fan-out | Major | NFR-1, FR-25, R-11 |
| SC-MCP-015 | Ordering of notifications | Per-stream order only, no global | Treat updated as "refetch authoritative state" (last-read-wins, read-after-notify) | Major | NFR-3, R-11, FR-32 |
| SC-MCP-016 | Session auth expiry mid-stream | OAuth token expires on long stream (401) | Detect 401; refresh/re-auth; re-establish+resubscribe; surface auth-required if refresh fails | Major | FR-4, FR-34, R-6 |
| SC-MCP-017 | notifications/message logging flood | High volume of `notifications/message` | Route to log sink at negotiated level; not a trigger unless mapped; rate-limit | Minor | FR-44, FR-39, —, propose FR |
| SC-MCP-018 | logging/setLevel not set | Some servers emit no logs until setLevel | Send `logging/setLevel` on connect if wanted; tolerate both | Minor | FR-44, —, propose FR |
| SC-MCP-019 | Refetch resources/read fails after notify | -32002 / -32603 / timeout post-notify | Retry backoff; if gone emit "update detected, content unavailable"; never drop/block | Major | FR-32, FR-27, NFR-7 |
| SC-MCP-020 | Duplicate subscribe / idempotency | Multiple downstream subscribers same uri | Refcount upstream — one subscribe per uri, fan out to N; unsubscribe at 0 | Major | FR-17, FR-32, R-5 |
| SC-MCP-021 | Multi-worker session affinity loss | streamable-HTTP session on worker A, routed to B → 404 | Shared session store or sticky routing; on 404 re-init+resubscribe; no orphaned subs | Critical | NFR-5, R-6, FR-32 |
| SC-MCP-022 | prompts/list_changed | `notifications/prompts/list_changed` | Invalidate cached prompts; re-fetch `prompts/list` | Minor | FR-44, R-6 |
| SC-MCP-023 | Notification for unsubscribed / unknown uri | Server bug / stale sub | Ignore/log; no fan-out; no auto-subscribe | Minor | FR-32, R-10 |

---

## 7. Coverage gaps — proposed new requirements

Scenarios above marked `—, propose FR` are not yet covered by an FRD requirement. Consolidated and de-duplicated, they suggest the following additions (numbering continues the FRD's `FR-44`/`NFR-9`). **The two Critical gaps are PG-FR-A (egress SSRF) and PG-FR-C (HMAC hardening)** — both map to the highest-severity scenarios and have no implementing requirement today.

### Security (highest priority)
- **PG-FR-A — Egress SSRF defense (Critical).** Outbound callback URLs MUST be validated (https-only scheme allowlist; deny RFC1918/loopback/link-local/multicast/cloud-metadata) on create, update, AND at delivery via resolve-once-and-pin (DNS-rebind/TOCTOU safe), with auto-redirects disabled. *(SC-SEC-020/021/022/023/024/025/026/055, SC-DEL-041, SC-SUB-048)*
- **PG-FR-B — Outbound callback signing (High).** Every outbound delivery MUST be HMAC-signed over id+timestamp+body so subscribers can verify gateway origin and detect replay. *(SC-SEC-051, SC-DEL-069)*
- **PG-FR-C — HMAC verification hardening (Critical).** Inbound verification MUST use constant-time comparison and a descriptor-pinned algorithm; payload-supplied `alg` MUST be ignored (CWE-347). *(SC-SEC-002/003, SC-ING-013)*
- **PG-FR-D — Anti-oracle uniform responses (High).** Ingress MUST return uniform responses and timing for unknown/invalid conn-ids and failed verification (CWE-203). *(SC-SEC-009/010)*
- **PG-FR-E — Capability-URL hygiene (High).** conn-id capability URLs MUST be high-entropy opaque, never in query strings, protected by `Referrer-Policy`, and support regenerate/revoke (CWE-598). *(SC-SEC-034/035/036/037)*
- **PG-FR-F — Stored-XSS / untrusted data at sinks (High).** Event-derived data rendered in Admin UI or forwarded to sinks MUST be context-encoded as untrusted. *(SC-SEC-041)*
- **PG-FR-G — Log-injection neutralization (Medium).** Logged event/header data MUST neutralize CR/LF and use structured logging (CWE-117). *(SC-SEC-038)*
- **PG-NFR-A — Secret rotation & revocation (High).** Signing secrets MUST support immediate revocation, last-used auditing, and dual-secret overlap rotation without dropping in-flight POSTs. *(SC-SEC-017/018, SC-ING-057)*
- **PG-NFR-B — DoS body/decompression limits (High).** Ingress MUST enforce a hard max body size (413 pre-buffer) and a per-codec decompressed-size cap (gzip/br/zstd/snappy) (CWE-400/770/409). *(SC-SEC-043/044, SC-ING-046, SC-DEL-072/073)*
- **PG-NFR-C — Rate limits & quotas (High).** Ingress and egress MUST enforce per-source/connector/tenant rate limits + per-tenant delivery quotas with circuit-breaking and anomaly alerts. *(SC-SEC-046/047/049)*
- **PG-FR-H — TLS on ingress (Major).** Inbound ingress MUST be served over TLS only; plaintext HTTP rejected/redirected. *(SC-ING-043)*

### Delivery & reliability
- **PG-NFR-D — Retry jitter (Major).** Retry schedules MUST apply randomized jitter to prevent synchronized retry storms. *(SC-DEL-016/047)*
- **PG-FR-I — HTTP response classification (Major).** The HTTP-callback adapter MUST classify responses — 4xx permanent (410→auto-disable), 5xx/429/timeout transient — and honor `Retry-After`. *(SC-DEL-019/020/021)*
- **PG-NFR-E — Per-endpoint circuit breaker (Major).** MUST open after N consecutive failures and half-open probe before resuming, isolating failing endpoints. *(SC-DEL-042/043)*
- **PG-NFR-F — Egress timeouts & response caps (Major).** Outbound delivery MUST enforce connect/total/idle timeouts (~15s) and cap response bytes (slowloris/oversized defense). *(SC-DEL-038/039)*
- **PG-NFR-G — Stream-trim safety & failover RPO (Critical).** Stream trimming (MAXLEN/MINID) MUST be sized to the slowest consumer's lag; Redis failover RPO (un-replicated tail) MUST be documented and bounded via AOF/persistence. *(SC-DEL-057/060)*
- **PG-FR-J — Reconciliation/backfill (Major).** SHOULD provide a path to detect and backfill missed provider events via the provider API within a retention window. *(SC-ING-030)*
- *(Extend FR-39 to include PEL entry age and DLQ depth alert thresholds — SC-DEL-055/045.)*

### Subscription / routing
- **PG-FR-K — Filter mutability model (Major).** A subscription's CEL filter is immutable post-create (recreate to change) OR mutable with a defined cut-over; pick and document. *(SC-SUB-005/019)*
- **PG-FR-L — Idempotent delete (Major).** `DELETE /subscriptions/{id}` MUST be idempotent — no double-decrement of the upstream refcount. *(SC-SUB-008)*
- **PG-FR-M — Bulk atomicity (Major).** Bulk operations MUST report per-item partial failure and MUST NOT leave half-registered upstream hooks. *(SC-SUB-011)*
- **PG-FR-N — Per-tenant subscription quota (Major).** Bounded by per-tenant quota; over-quota create returns 429 with no partial registration. *(SC-SUB-013)*
- **PG-FR-O — CEL validating admission + cost budget (Critical).** CEL filters MUST pass create-time admission (syntax, type-check, static cost, depth cap) + a runtime cost budget that halts over-budget evaluation fail-closed. *(SC-SUB-034/038/039/041)*
- **PG-FR-P — Refcount reconciliation loop (Major).** Periodically compare live subscriptions vs registered upstream hooks and repair/meter drift. *(SC-SUB-031)*
- **PG-FR-Q — Unmatched-event handling (Minor).** Events matching zero subscriptions MUST be dropped with a debug log + `unmatched` meter, optional archive. *(SC-SUB-030)*
- *(Also: paused-sub refcount policy SC-SUB-014; payload-version pinning SC-SUB-016; subscribe-to-non-emitted-type SC-SUB-028; contradiction-filter warning SC-SUB-045; DLQ-of-DLQ gap SC-SUB-051; dedup-by-hash policy SC-SUB-010.)*

### Correlate / async
- **PG-FR-R — Correlate TTL semantics (Major).** Correlate subscriptions MUST carry a configurable TTL; on expiry the run is resolved timed-out and later completions are rejected/dead-lettered, never resumed. *(SC-COR-006/007/021)*
- **PG-FR-S — Task poll loop (Major).** For async tools, maintain a durable `tasks/get` poll loop (pollInterval authoritative) with jittered backoff, concurrency caps, per-taskId coalescing, independent of optional status notifications. *(SC-COR-006/018/022/023)*
- **PG-FR-T — Task-state monotonicity (Major).** Enforce monotonic transitions via lastUpdatedAt; terminal states sticky; regressions ignored+logged. *(SC-COR-014/015)*
- **PG-FR-U — Auth-context binding of correlation id (Critical).** Every taskId/correlation_value bound to its originating team/owner; foreign-context completions rejected. *(SC-COR-012)*
- **PG-FR-V — Durable pending-run map (Critical).** The pending-run↔taskId map MUST be persisted durably; on restart re-poll known taskIds (no `tasks/list` in the RC). *(SC-COR-025/026)*
- **PG-FR-W — Server-directed task detection (Major).** Detect a `CreateTaskResult` on any `tools/call` and switch that call to async/correlate mode. *(SC-COR-024)*
- **PG-FR-X — input_required continuity (Major).** Surface `input_required` to the agent/user and answer via `InputRequiredResult`+state token while keeping the correlation alive. *(SC-COR-017)*

### MCP-native
- **PG-FR-Y — Missed-update reconciliation (Major).** On session re-establishment, proactively refetch all subscribed resources + re-list, emitting a synthetic "may-have-missed" event; never silently drop gap updates. *(SC-MCP-006)*
- **PG-FR-Z — Subscription polling fallback (Major).** When `resources.subscribe` is absent/false or rejected, degrade to polling/listChanged, record the mismatch, surface "no live updates", don't crash. *(SC-MCP-007/008)*
- **PG-FR-AA — MCP logging routing (Minor).** `notifications/message` routes to a rate-limited log sink at the negotiated level and is not a trigger unless explicitly mapped; MAY send `logging/setLevel` on connect; tolerate servers logging before/without it. *(SC-MCP-017/018)*
- *(Also: make explicit in FR-23/FR-32 that the synthesized id substitutes for the absent provider `event.id` in the dedup key — SC-MCP-002/003, R-10; and note under R-11 that MCP updated events are coalescible/last-read-wins, unlike at-least-once external deliveries — SC-MCP-014/015.)*

---

## 8. Sources

Inbound ingestion & provider behavior — GitHub, Stripe, Slack, Shopify, Twilio, GitLab, PagerDuty webhook docs; [Hookdeck](https://hookdeck.com/webhooks/guides), [Svix](https://docs.svix.com), [ngrok webhook security](https://ngrok.com/blog/get-webhooks-secure-it-depends-a-field-guide-to-webhook-security), [webhooks.fyi](https://webhooks.fyi).
Delivery reliability & Redis Streams — [Hookdeck delivery guarantees/retries/DLQ](https://hookdeck.com/webhooks/guides/webhook-delivery-guarantees), [Svix retries/ordering](https://www.svix.com/blog/guaranteeing-webhook-ordering/), [AWS EventBridge retry/DLQ](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-dlq.html), [AWS SNS retries](https://docs.aws.amazon.com/sns/latest/dg/sns-message-delivery-retries.html), [Redis Streams](https://redis.io/docs/latest/develop/data-types/streams/) + XREADGROUP/XACK/XPENDING/XCLAIM/XAUTOCLAIM/XTRIM, [antirez consumer patterns](https://redis.antirez.com/fundamental/streams-consumer-patterns.html), [Convoy circuit breaker](https://www.getconvoy.io/blog/circuit-breaker-in-golang).
Security & abuse — [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html), [OWASP Multi-Tenant](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html), [OWASP Webhook Security (draft)](https://github.com/OWASP/CheatSheetSeries/blob/master/cheatsheets_draft/Webhook_Security_Guidelines_Cheat_Sheet.md), [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md), [Svix zero-downtime rotation](https://www.svix.com/blog/zero-downtime-secret-rotation-webhooks/), CWE-347/117/409/770/598/203/400, [PortSwigger JWT algorithm confusion](https://portswigger.net/web-security/jwt/algorithm-confusion).
Subscription lifecycle, routing & CEL — [Knative Eventing filtering](https://github.com/knative/eventing/blob/main/docs/broker/filtering.md), [CloudEvents Subscriptions API](https://github.com/cloudevents/spec/blob/main/subscriptions/spec.md), [Google Pub/Sub filters/dead-letter](https://docs.cloud.google.com/pubsub/docs/subscription-message-filter), [CEL language definition](https://github.com/google/cel-spec/blob/master/doc/langdef.md), [Kubernetes CEL cost limits](https://kubernetes.io/docs/reference/using-api/cel/), [Azure Event Grid endpoint validation](https://learn.microsoft.com/en-us/azure/event-grid/end-point-validation-cloud-events-schema), [W3C WebSub](https://www.w3.org/TR/websub/).
Async/correlate & MCP-native — [MCP Tasks (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks), [2026-07-28 Release Candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/), [SEP-1686 Tasks](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686), [Webhooks for Operations (#523)](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/523), [MCP Resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources), [MCP Transports](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports), [Hookdeck MCP event gateway](https://hookdeck.com/blog/mcp-event-gateway).
