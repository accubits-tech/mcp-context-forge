# MCP Triggers & Events — Test Cases

> **Status:** Draft for review &nbsp;·&nbsp; **Date:** 2026-05-30 &nbsp;·&nbsp; **Companions:** [`frd-mcp-triggers-events.md`](./frd-mcp-triggers-events.md) (requirements) · [`mcp-triggers-events-scenarios.md`](./mcp-triggers-events-scenarios.md) (scenarios)
>
> Test cases derived from the scenario catalog. Every test references the scenario(s) it covers (`Covers` column → `SC-<CAT>-NNN`), giving end-to-end traceability **FRD requirement → scenario → test**. ~270 cases across six categories.

## How to read this document

- **ID scheme:** `TC-<CAT>-NNN`, same category prefixes as the scenarios. `Covers` links to `SC-*` ids; `(neg)` marks an adversarial/negative case.
- **Level:** `unit` (pure logic, no I/O), `integration` (component + Redis/DB/mock provider), `e2e` (full ingress→delivery→downstream), `security` (adversarial), `load` (throughput/latency), `chaos` (crash/failover/kill).
- **Priority (normalized):** **P0** = must pass to ship the gating milestone (data-loss / security / money); **P1** = important; **P2** = should; **P3** = nice-to-have. *(Some tables below use High/Med/Critical/Major/Minor in the Pri column — read High≈P1, Med≈P2, Critical≈P0, Major≈P1, Minor≈P3.)*
- **Milestone gating** (see §8): ING→M1; DEL→M2/M3; SUB→M2; SEC→cross-cutting M1–M5; COR→M7; MCP→M6.

## Required test infrastructure

A shared harness underpins these cases (build once, reuse across categories):

- **Provider signature fixtures** — signed sample payloads per provider: GitHub (`push`/`pull_request`, sha256 + legacy sha1, `ping`, `X-GitHub-Delivery`), Stripe (`t=,v1=` single + multi-`v1` rotation, duplicate `id`, twin events, thin payload), Slack (`url_verification` challenge, `v0:{ts}:{body}`, `event_id`+`X-Slack-Retry-Num`), Shopify (base64 HMAC, `X-Shopify-Webhook-Id`, shared `X-Shopify-Event-Id`, 3xx-retry), Twilio (form sorted-param URL sig, JSON `bodySHA256`), GitLab (plaintext token + HMAC), PagerDuty (CSV `v1=a,v1=b`). Plus edge fixtures: malformed JSON, >25 MB body, empty/null body, mixed-case/duplicate headers, no-event-id payload, behind URL-rewriting proxy.
- **Fake subscriber harness** — configurable HTTP sink: 2xx / 4xx / 410 / 429+Retry-After / 5xx; slow / hang / slowloris trickle; 302→private redirect; decompression-bomb / huge body; 200-with-error-body; commit-then-drop-2xx; signature-verifying receiver (for outbound-signing tests).
- **Redis with AOF/persistence + primary→replica failover** harness (chaos).
- **Multi-worker harness** — N workers in one consumer group; kill/scale-down on demand; inspect PEL via `XPENDING`/`XINFO`.
- **Load / fan-out generator** — provider burst, high-cardinality subscriptions, lag/metric scraping.
- **Clock-skew injection** — manipulate worker/Redis/provider time.
- **CEL test corpus** — valid / malformed / type-mismatch / missing-field / null-vs-absent / deep-nesting / short-circuit / cost-budget / glob-anchoring / ReDoS expressions.
- **Ref-count reconciliation harness** — fake provider hook registry that injects drift (orphan hooks, missing hooks, refcount skew) + concurrent sub/unsub stress.
- **Mock upstream MCP server** — emits `notifications/resources/updated` (uri-only), `*/list_changed`, `notifications/message`; supports session 404, Last-Event-ID, capability toggles, `-32002`/`-32603`, OAuth-expiry.
- **Tolerant-parser fixtures** — `CreateTaskResult`/status with renamed/extra/aliased fields (`taskId`/`id`, `status`/`state`) for the provisional `#523`/Tasks identifiers.

---

## 1. ING — Inbound ingestion tests

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-ING-001 | SC-ING-001 | integration | GitHub conn + secret | POST signed GitHub push fixture w/ valid `X-Hub-Signature-256` | 202; event `type=com.github.push` on bus | P1 |
| TC-ING-002 | SC-ING-001, 056 | security | GitHub conn | POST push w/ tampered signature | 401; no event emitted | P1 |
| TC-ING-003 | SC-ING-002 | integration | GitHub legacy sha1 recipe | POST push w/ only `X-Hub-Signature: sha1=` valid | 202; event emitted | P2 |
| TC-ING-004 | SC-ING-003 | integration | Stripe conn + secret | POST Stripe fixture; sig over `{t}.{body}`, valid `v1` | 202; `type=com.stripe.*` | P1 |
| TC-ING-005 | SC-ING-003 | security | Stripe conn | POST w/ valid `v0` but bad `v1` | 401 (v0 ignored) | P1 |
| TC-ING-006 | SC-ING-004, 057 | integration | Stripe 2 secrets (rotation) | POST w/ two `v1=`, one matches old secret | 202; accepted | P2 |
| TC-ING-007 | SC-ING-005 | integration | Slack conn + signing secret | POST Slack event; base `v0:{ts}:{body}`, valid sig | 202; event emitted | P1 |
| TC-ING-008 | SC-ING-006 | integration | Shopify conn + app secret | POST fixture; valid base64 `X-Shopify-Hmac-Sha256` | 202; `type=com.shopify.*` | P1 |
| TC-ING-009 | SC-ING-006 | security | Shopify conn | POST w/ hex sig where base64 expected | 401 | P2 |
| TC-ING-010 | SC-ING-007 | integration | Twilio conn (plugin recipe) | POST form params; sig over URL+sorted params | 202; event emitted | P2 |
| TC-ING-011 | SC-ING-008 | integration | Twilio JSON conn | POST JSON w/ `bodySHA256` query + valid URL sig | 202; event emitted | P2 |
| TC-ING-012 | SC-ING-009 | integration | PagerDuty conn 2 secrets | POST w/ CSV `v1=a,v1=b`, b matches | 202; accepted | P2 |
| TC-ING-013 | SC-ING-010 | integration | GitLab token conn | POST w/ matching `X-Gitlab-Token` | 202; event emitted | P2 |
| TC-ING-014 | SC-ING-010 | security | GitLab token conn | POST w/ wrong `X-Gitlab-Token` | 401 | P2 |
| TC-ING-015 | SC-ING-011 | integration | GitLab HMAC conn | POST w/ valid GitLab HMAC sig | 202; event emitted | P3 |
| TC-ING-016 | SC-ING-012 | security | Conn w/ base64 recipe | POST hex-encoded sig | 401; encoding-mismatch logged | P2 |
| TC-ING-017 | SC-ING-013 | security | Any HMAC conn | Static/timing test of compare fn | Uses `hmac.compare_digest`; no early return | P1 |
| TC-ING-018 | SC-ING-014, 060 | security | Body-parser middleware on | POST signed body; assert raw bytes captured pre-parse | 202; verify uses unmodified raw body | P1 |
| TC-ING-019 | SC-ING-015 | integration | Two conns, distinct secrets | POST to conn-A signed w/ conn-B secret | 401; secret resolved by conn-id | P1 |
| TC-ING-020 | SC-ING-016 | integration | Slack conn verified | POST `url_verification` w/ valid sig | 200; body echoes `challenge` | P1 |
| TC-ING-021 | SC-ING-016 | security | Slack conn | POST `url_verification` w/ bad sig | 401; challenge NOT echoed (no oracle) | P1 |
| TC-ING-022 | SC-ING-017 | integration | GitHub conn | POST `X-GitHub-Event: ping` valid sig | 2xx; no domain event on bus | P2 |
| TC-ING-023 | SC-ING-018 | integration | Ingress route live | GET and HEAD `/webhooks/{conn-id}` | 200; no event emitted | P3 |
| TC-ING-024 | SC-ING-019 | e2e | New subscription flow | Subscribe; provider probes URL | Probe answered; subscription completes | P2 |
| TC-ING-025 | SC-ING-020 | integration | GitHub conn | POST same `X-GitHub-Delivery` twice | First 202+emit; second deduped | P1 |
| TC-ING-026 | SC-ING-021 | integration | Stripe conn | POST same Stripe `id` twice | Processed once | P1 |
| TC-ING-027 | SC-ING-022 | integration | Stripe conn | POST twin events same `type`+`object.id` | Deduped to one | P3 |
| TC-ING-028 | SC-ING-023 | integration | Slack conn | POST same `event_id`, `X-Slack-Retry-Num:1` | Deduped; retry header honored | P2 |
| TC-ING-029 | SC-ING-024 | integration | Shopify conn | POST same `X-Shopify-Webhook-Id` twice | Processed once | P2 |
| TC-ING-030 | SC-ING-025 | integration | Shopify conn, 2 topics | POST 2 topics sharing `X-Shopify-Event-Id` | Both processed (topic in dedup key) | P2 |
| TC-ING-031 | SC-ING-026 | unit | Dedup cache config | Set TTL < provider retry window | Config rejected/warns; TTL ≥ window | P2 |
| TC-ING-032 | SC-ING-027 | unit | Provider w/o event id | Ingest payload lacking id | Dedup key = hash(body+headers) | P3 |
| TC-ING-033 | SC-ING-028 | integration | Slow downstream | POST event; provider retries during async work | Idempotent; single side-effect | P1 |
| TC-ING-034 | SC-ING-029 | integration | Provider unordered | POST events out of timestamp order | All accepted; downstream orders by stream id | P2 |
| TC-ING-035 | SC-ING-030 | e2e | Reconcile job configured | Simulate dropped event; run reconcile via API | Gap detected and backfilled | P3 |
| TC-ING-036 | SC-ING-031 | unit | State w/ newer `updated_at` | Ingest older event version | Stale event ignored | P3 |
| TC-ING-037 | SC-ING-032 | integration | Ingest fails internally | POST valid event; force 5xx then redeliver | Provider retry deduped; eventually once | P2 |
| TC-ING-038 | SC-ING-033, 052 | load | Healthy receiver | Measure verify→202 latency under load | 202 within provider window (e.g. <3s Slack) | P1 |
| TC-ING-039 | SC-ING-034 | unit | Per-provider config | Assert dedup TTL/DLQ tuned per Stripe/Shopify/GitLab | Values cover provider retry windows | P3 |
| TC-ING-040 | SC-ING-035 | integration | Metrics enabled | Drive sustained rejects | reject-rate metric rises; alert fires | P2 |
| TC-ING-041 | SC-ING-036, 054 | load | Bus/DB saturated | Burst beyond capacity | 429/503 returned; no data loss; provider retries | P1 |
| TC-ING-042 | SC-ING-037 | integration | Endpoint re-enabled | Re-enable after disable; provider replays | Backlog ingested idempotently | P3 |
| TC-ING-043 | SC-ING-038 | security | hmac_timestamped conn | Replay captured request after tolerance | 401/reject (replay) | P1 |
| TC-ING-044 | SC-ING-039 | security | Stripe conn, tol=300s | POST ts 6 min old (valid sig) | Reject; ts within 300s accepted | P1 |
| TC-ING-045 | SC-ING-040 | security | Slack conn, tol=300s | POST ts >300s old | Reject as replay | P1 |
| TC-ING-046 | SC-ING-039 | unit | Tolerance config | Set tolerance=0 | Rejected by validation (never 0) | P2 |
| TC-ING-047 | SC-ING-041 | unit | Clock skew sim | Skew gateway clock within tolerance | Event still accepted | P3 |
| TC-ING-048 | SC-ING-042 | security | IP allowlist on | POST from non-allowlisted IP | Rejected (defense-in-depth) | P3 |
| TC-ING-049 | SC-ING-043 | security | TLS enforced | POST over plain HTTP | Rejected/redirected to HTTPS | P2 |
| TC-ING-050 | SC-ING-044 | integration | Any conn | POST valid sig + malformed JSON | Verify passes; 400; no retry-loop trigger | P1 |
| TC-ING-051 | SC-ING-044 | security | Any conn | POST malformed JSON + bad sig | 401 before parse attempt | P1 |
| TC-ING-052 | SC-ING-045 | integration | Form-capable conn | POST form-encoded + `json; charset=utf-8` | Both parsed/normalized correctly | P3 |
| TC-ING-053 | SC-ING-046 | security | Size limit set | POST body > limit (>25 MB) | Rejected (413); not truncated | P2 |
| TC-ING-054 | SC-ING-047 | integration | Subs for known types | POST unknown event type, valid sig | 200/202 + ignored; no error | P2 |
| TC-ING-055 | SC-ING-048 | unit | Known type normalizer | Ingest known type w/ new `action` value | Normalized; no crash | P3 |
| TC-ING-056 | SC-ING-049 | unit | Version-skew payloads | Ingest payloads w/ differing API-version header | Normalized; version recorded | P3 |
| TC-ING-057 | SC-ING-050 | integration | Thin-payload provider | Ingest thin event | Accepted; downstream API-fetch path triggered | P3 |
| TC-ING-058 | SC-ING-051 | integration | Any conn | POST empty body and `null` body (valid sig) | 400 (no body) / probe→200 as configured | P2 |
| TC-ING-059 | SC-ING-052 | integration | Async worker on | POST event; assert 202 before delivery work | 202 returned pre-matching; work queued | P1 |
| TC-ING-060 | SC-ING-053 | load | L2 stream up | Fire burst (~10k events) | All XADD'd; ingress latency stable; no backpressure | P1 |
| TC-ING-061 | SC-ING-055 | integration | DLQ configured | Force event un-processable after retries | Lands in `dead_letters`; secrets redacted | P2 |
| TC-ING-062 | SC-ING-056 | security | Any conn | POST with no signature header | 401 (fail closed) | P1 |
| TC-ING-063 | SC-ING-056 | security | Any conn | POST with garbled/empty sig header | 400 (malformed) | P1 |
| TC-ING-064 | SC-ING-057 | integration | Rotation overlap window | POST signed w/ old secret while new active | Accepted during overlap | P2 |
| TC-ING-065 | SC-ING-058 | unit | Sig header lookup | POST duplicate + mixed-case sig headers | Case-insensitive; defined precedence; correct verify | P3 |
| TC-ING-066 | SC-ING-059 | integration | Reverse proxy in front | POST through proxy that rewrites URL | Verify over original URL/raw body; accepted | P2 |
| TC-ING-067 | SC-ING-060 | security | CSRF middleware enabled | POST signed webhook through CSRF/body middleware | Raw body intact; verify passes; CSRF not applied to webhook route | P1 |
| TC-ING-068 | SC-ING-061 | integration | GitHub conn | POST GitHub push fixture | Envelope has id/source/type(reverse-DNS)/subject/time/data | P1 |
| TC-ING-069 | SC-ING-061 | unit | Normalizer unit | Feed Stripe/Slack/Shopify fixtures to normalizer | Each maps to correct envelope fields | P1 |

---

## 2. DEL — Delivery & bus tests

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-DEL-001 | SC-DEL-010 | unit | Event w/ bound sub | Create delivery; trigger 3 retries | Same Idempotency-Key=delivery_id on all attempts | P0 |
| TC-DEL-002 | SC-DEL-002,075 | integration | Fake subscriber flaps 503→200 | Deliver; force retry storm | Receiver sees N POSTs, all same key; dedupes to one effect | P0 |
| TC-DEL-003 | SC-DEL-003,011 | integration | Processed table TTL > retry horizon | Deliver, wait past in-mem window, redeliver same key | Persistent table catches dup; one effect; key expires after TTL | P1 |
| TC-DEL-004 | SC-DEL-004 | integration | 2 workers, same delivery_id | Process concurrently | Unique constraint/ON CONFLICT → exactly one row | P0 |
| TC-DEL-005 | SC-DEL-005,022,044 | unit | Receiver 200 w/ error body / acks-then-crashes | Deliver | Gateway records success (status authoritative); no retry | P2 |
| TC-DEL-006 | SC-DEL-006 | unit | Same business key, two event.ids | Deliver both | Wire dedup misses; semantic dedup on business key collapses | P2 |
| TC-DEL-007 | SC-DEL-008 | integration | Two subs match one event | Ingress event | Two distinct deliveries, two delivery_ids, two queues | P1 |
| TC-DEL-008 | SC-DEL-015,016 | unit | Backoff config | Fail repeatedly | Intervals grow exponentially; jitter applied; bounded by max | P1 |
| TC-DEL-009 | SC-DEL-017,024,028 | integration | maxAttempts=N, always-fail subscriber | Exhaust retries | Lands in dead_letters w/ context; alert/metric; no silent drop | P0 |
| TC-DEL-010 | SC-DEL-019,020 | unit | Subscriber returns 400/410/500 | Deliver each | 4xx permanent (no/limited retry); 410 auto-disables; 5xx retried | P0 |
| TC-DEL-011 | SC-DEL-021 | integration | Subscriber 429 Retry-After: 30 | Deliver | Next attempt ~30s (capped); honored | P1 |
| TC-DEL-012 | SC-DEL-026,029,066 | integration | Items in DLQ / stream history | Bulk replay | Rate-limited; original delivery_id preserved; receiver dedupes | P1 |
| TC-DEL-013 | SC-DEL-025,070 | integration | DLQ depth threshold; attempts logged | Push past threshold | Alert fires; delivery_attempts has raw body+headers (secrets redacted) | P1 |
| TC-DEL-014 | SC-DEL-031,033,035 | integration | Per-sub per-subject queue | Deliver seq within subject, force reorder | Within subject ordered; stale (lower version) discarded | P1 |
| TC-DEL-015 | SC-DEL-032,071 | integration | Sub A slow, B fast; subject X slow/Y fast | Ingress events both | B/Y unblocked; A/X isolated; no head-of-line block | P0 |
| TC-DEL-016 | SC-DEL-018,042,043,047 | integration | Zombie endpoint, retry budget set | Fail past N | Circuit opens; retry rate capped; half-open probe closes on recovery | P1 |
| TC-DEL-017 | SC-DEL-036,037,038,039 | integration | Fake subscriber: refused/bad-cert/hang/slowloris | Deliver each | Conn-refused/timeout retried; bad cert fails closed; slowloris aborted on idle/total timeout + byte cap | P0 |
| TC-DEL-018 | SC-DEL-041 | security | Fake subscriber 302→http://169.254.169.254 | Deliver | Redirect to private/link-local not followed; SSRF blocked | P0 |
| TC-DEL-019 | SC-DEL-072,073 | unit | Decompression-bomb response; deeply nested JSON | Deliver/parse | Decompressed size capped per codec; nesting-depth limit rejects; no OOM/stack blowup | P1 |
| TC-DEL-020 | SC-DEL-050 | chaos | Single worker | Kill gateway after persist+XADD, before any XREADGROUP | On restart entry consumed from stream; delivered once; no loss | P0 |
| TC-DEL-021 | SC-DEL-051,055 | chaos | Worker consuming group | Kill worker after XREADGROUP before XACK | Entry in PEL; XAUTOCLAIM reclaims; delivered once more; PEL drains after XACK | P0 |
| TC-DEL-022 | SC-DEL-052,053 | integration | Two workers, min-idle-time set | Stall consumer 1 past min-idle; both attempt claim | XAUTOCLAIM reassigns to worker 2; no double-claim | P1 |
| TC-DEL-023 | SC-DEL-054 | unit | PEL references trimmed entry | Run XAUTOCLAIM | Trimmed id skipped + purged from PEL; no error | P2 |
| TC-DEL-024 | SC-DEL-056,057,058 | integration | XADD MAXLEN ~ sized to slowest-lag | Flood stream | Memory bounded; un-consumed backlog within slowest-consumer lag not trimmed; overshoot tolerated | P1 |
| TC-DEL-025 | SC-DEL-059 | integration | Group w/ dead consumer holding PEL | Run reaper | Dead consumer's PEL reclaimed then DELCONSUMER; no orphan entries | P2 |
| TC-DEL-026 | SC-DEL-060 | chaos | Redis primary+replica, AOF on | Kill primary mid-stream, promote replica | Persisted entries survive; only un-replicated tail lost; RPO within bound | P0 |
| TC-DEL-027 | SC-DEL-061,062,063 | chaos | 3 workers one group, load running | Scale down one worker mid-flight | No entry double-delivered; survivors XAUTOCLAIM dead PEL; balanced | P0 |
| TC-DEL-028 | SC-DEL-064 | integration | HTTP-callback group + SSE/WS group on same stream | Ingress events | Both groups consume independently; pace divergence ok | P2 |
| TC-DEL-029 | SC-DEL-048,049,077 | load | High-cardinality fan-out (~5k subs), burst | Drive provider burst | Ingress stays 202-fast; per-sub isolation; queue-first; no drop, no backpressure | P0 |
| TC-DEL-030 | SC-DEL-045,046 | load | L2 depth metrics + alert ~70% | Ramp load past 70% | Lag metric climbs; alert fires; backpressure (429) signaled, not silent drop | P1 |
| TC-DEL-031 | SC-DEL-012 | integration | Payment/charge target, dedup on | Deliver charge event twice (retry) | Single fulfillment; no double charge | P0 |
| TC-DEL-032 | SC-DEL-013,074 | unit | Delta vs absolute event variants | Apply duplicate/out-of-order | Absolute/re-fetch path converges; delta path documented-risky | P2 |
| TC-DEL-033 | SC-DEL-014 | unit | Two subs/types | Generate keys | Keys namespaced per subscription+type; no collision | P2 |
| TC-DEL-034 | SC-DEL-069 | security | Signed delivery envelope w/ ts+nonce | Replay old signed POST to receiver harness | Outside tolerance or reused nonce → rejected | P1 |
| TC-DEL-035 | SC-DEL-068 | unit | Skewed clocks worker/Redis | Order events | Ordering follows Redis stream id, not wall-clock | P2 |
| TC-DEL-036 | SC-DEL-067,027 | integration | Retention/TTL configured | Age events past window | Expired per retention; near-expiry metric; RPO documented | P2 |
| TC-DEL-037 | SC-DEL-076 | integration | SSE/WS sub + HTTP-callback sub | Drop SSE client; fail HTTP sub | SSE/WS best-effort (no DLQ); HTTP-callback dead-letters; divergence as documented | P1 |
| TC-DEL-038 | SC-DEL-023 | integration | Subscriber drops conn mid response | Deliver | Partial write → failure → retry; idempotency collapses dup | P2 |
| TC-DEL-039 | SC-DEL-009 | integration | Receiver commits then 2xx lost | Deliver | Gateway retries; receiver dedupes; net one effect | P1 |
| TC-DEL-040 | SC-DEL-030,034 | unit | Cross-topic / cross-sub events | Assert ordering contract | No global ordering promised; docs/test assert order-independence | P2 |

---

## 3. SEC — Security tests

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-SEC-001 | SC-SEC-001 | security | hmac recipe, prod profile | POST with no signature header | 401/400; not persisted; `none` recipe refused at config-load in prod | P0 |
| TC-SEC-002 | SC-SEC-002 | unit | HMAC verify fn | Feed valid-prefix wrong-suffix sigs; measure timing | Uses `compare_digest`; no byte-position timing variance | P1 |
| TC-SEC-003 | SC-SEC-003 | security | Signed connector | Send payload w/ `alg:none`/`HS1`/`md5` claim + forged sig | Rejected; verifier ignores payload alg (CWE-347) | P0 |
| TC-SEC-004 | SC-SEC-004 | integration | Two connectors A,B distinct secrets | Sign with A's secret, POST to B's conn-id | Rejected; secret selected by route not payload | P0 |
| TC-SEC-005 | SC-SEC-005 | unit | Verifier + raw-body capture | Send reordered keys/extra whitespace, valid sig | Verified over raw bytes (pass); re-serialized would fail | P1 |
| TC-SEC-006 | SC-SEC-006 | security | hmac_timestamped connector | Keep body, alter id/timestamp header, reuse old sig | Rejected; signature binds id+ts+body | P0 |
| TC-SEC-007 | SC-SEC-007 | security | Connector, no IP allowlist | POST unsigned from arbitrary IP | Rejected (secret required); off-list IP rejected w/ allowlist | P0 |
| TC-SEC-008 | SC-SEC-008 | integration | DB + dispatch observable | POST invalid signature; query event_log + L2 | No row, no XADD, no handshake echo before verify | P0 |
| TC-SEC-009 | SC-SEC-009 | security | Slack-style connector | Send `url_verification` challenge w/ bad/no sig | Challenge NOT echoed until verified+attributed (CWE-203) | P0 |
| TC-SEC-010 | SC-SEC-010 | security | Valid + bogus conn-ids | POST to existing vs nonexistent conn-id, invalid sig | Identical status/body/timing; no existence oracle | P1 |
| TC-SEC-011 | SC-SEC-011 | security | hmac_timestamped, 5min tolerance | Capture valid POST, replay after window | Rejected stale; in-window duplicate dedup-dropped | P0 |
| TC-SEC-012 | SC-SEC-012 | unit | Config loader | Set tolerance=0 then =6h | Both rejected/clamped; sane default applied | P2 |
| TC-SEC-013 | SC-SEC-013 | integration | Dedup store TTL set | POST same `event.id` twice | First once; second dedup-dropped; key `{team_id}:{conn_id}:{event.id}` | P0 |
| TC-SEC-014 | SC-SEC-014 | integration | Two tenants, same connector type | Tenant A `event.id=X`; tenant B replays X | B not blocked by A's nonce; store tenant-scoped | P1 |
| TC-SEC-015 | SC-SEC-015 | security | DB access | Inspect `webhook_signing_secret` column at rest | Ciphertext only; no plaintext | P0 |
| TC-SEC-016 | SC-SEC-016 | security | Repo + support bundle | Run secret scan; generate support bundle | No secrets in VCS; bundle redacts secrets | P1 |
| TC-SEC-017 | SC-SEC-017 | integration | Dual-secret rotation | Rotate; POST signed w/ old then new during overlap | Both accepted in window; old rejected after | P1 |
| TC-SEC-018 | SC-SEC-018 | integration | Active connector | Revoke/expire secret via API | Subsequent POSTs w/ that secret rejected immediately; last-used audited | P1 |
| TC-SEC-019 | SC-SEC-019 | security | Two tenants | Attempt same secret for both | Per-tenant secret enforced; reuse denied/isolated | P1 |
| TC-SEC-020 | SC-SEC-020 | security | Subscription create + delivery | Register `callback_url` = `10.0.0.5`/`127.0.0.1`/`[::1]`/`169.254.x` | Rejected at create AND at delivery (pin-and-connect) | P0 |
| TC-SEC-021 | SC-SEC-021 | security | Subscription create | `callback_url`→`169.254.169.254/...`, `metadata.google.internal` | Rejected at create and delivery; egress proxy denies | P0 |
| TC-SEC-022 | SC-SEC-022 | security | URL validator | `2130706433`/`0x7f.1`/`0177.0.0.1`/`[::ffff:169.254.169.254]`/`user@evil@127.0.0.1` | All canonicalized and rejected | P0 |
| TC-SEC-023 | SC-SEC-023 | security | Controllable DNS for callback host | Host public at create, flips to 127.0.0.1 at delivery | Delivery resolves once + pins + validates at connect; blocked (rebind) | P0 |
| TC-SEC-024 | SC-SEC-024 | integration | Callback returns 302 | Endpoint 302→`169.254.169.254` | Redirect not followed (or re-validated+blocked) | P0 |
| TC-SEC-025 | SC-SEC-025 | security | Subscription create | `callback_url` = `file://`/`gopher://`/`ftp://`/`dict://`/`http://` | All rejected; https-only allowlist | P0 |
| TC-SEC-026 | SC-SEC-026 | security | Subscription create | `callback_url=https://internal:8080/` non-443 | Rejected unless port allowlisted; egress firewall | P1 |
| TC-SEC-027 | SC-SEC-027 | integration | Callback returns body/headers | Subscriber returns secret-looking body | Gateway logs status/size only; upstream body not reflected/stored | P2 |
| TC-SEC-028 | SC-SEC-028 | security | Tenant A sub id, tenant B token | GET/PUT/DELETE `/subscriptions/{A-id}` as B | 403/404; object-level authz `team_id` (BOLA/IDOR) | P0 |
| TC-SEC-029 | SC-SEC-029 | integration | Event for A, sub in B matches filter | Ingest A event; run worker | Delivered only to A; worker asserts tenant; B not delivered | P0 |
| TC-SEC-030 | SC-SEC-030 | security | Low-priv user token | Create/delete subscription without role | Rejected; role + object-level authz enforced | P1 |
| TC-SEC-031 | SC-SEC-031 | security | Tenant lacking event-type entitlement | Subscribe to unentitled event-type | Rejected; entitlement check (confused deputy) | P1 |
| TC-SEC-032 | SC-SEC-032 | integration | Multi-instance, shared cache/pool | Interleave two tenants through L1 cache/session | No cross-tenant bleed; cache keyed by tenant; session reset | P1 |
| TC-SEC-033 | SC-SEC-033 | security | Tenant B sub referencing A's connector | Create sub w/ A's conn_id as B | Rejected; tenant-scoped connector reference | P1 |
| TC-SEC-034 | SC-SEC-034 | security | Generated conn-ids | Generate many; measure entropy/sequence | High-entropy opaque, non-guessable; auth still required | P1 |
| TC-SEC-035 | SC-SEC-035 | security | Capability URL handling | Pass conn-id in query; trigger outbound nav | Id in path/header not query; `Referrer-Policy` set; logs redact (CWE-598) | P1 |
| TC-SEC-036 | SC-SEC-036 | unit | Log formatter | Request w/ `?secret=...` / Authorization header | Query secret + Authorization redacted in logs | P1 |
| TC-SEC-037 | SC-SEC-037 | integration | Existing connector | Call regen/revoke conn-id endpoint | New conn-id issued; old 404; deliveries unaffected | P2 |
| TC-SEC-038 | SC-SEC-038 | security | Log sink | Field/header w/ `\r\n INJECTED admin login` | Logged escaped single record; no forged line (CWE-117) | P1 |
| TC-SEC-039 | SC-SEC-039 | security | DLQ + event_log + logs | Trigger failed delivery carrying credentials | Secrets/Authorization redacted in DLQ, event_log, logs | P0 |
| TC-SEC-040 | SC-SEC-040 | integration | PII in payload | Ingest event w/ email/SSN-like fields | Persisted redacted/minimized; retention; encrypted at rest | P1 |
| TC-SEC-041 | SC-SEC-041 | security | Admin UI sub/event view | Ingest event w/ `<script>`/template; open Admin UI | Rendered escaped; no stored XSS | P0 |
| TC-SEC-042 | SC-SEC-042 | security | Ingress | Send malformed body to force parse error | Generic error; no stack trace/internals | P2 |
| TC-SEC-043 | SC-SEC-043 | security | Body limit configured | POST body exceeding hard max | `413` before full buffering; not OOM | P0 |
| TC-SEC-044 | SC-SEC-044 | security | Compression accepted | POST 1KB gzip→1GB (and br/zstd/snappy) | Aborted at decompressed cap per codec (CWE-409); 413/400 | P0 |
| TC-SEC-045 | SC-SEC-045 | security | Ingress | Duplicate/CRLF/oversized headers, smuggling probe | Stripped/rejected; header allowlist enforced | P1 |
| TC-SEC-046 | SC-SEC-046 | integration | Rate limits configured | Flood POSTs per source/connector/tenant | Throttled (429); unverified rejected cheaply; fan-out capped | P1 |
| TC-SEC-047 | SC-SEC-047 | integration | Dead subscriber endpoint | Point sub at hanging endpoint; send N events | Per-delivery timeout; circuit-breaker opens; per-tenant quota; others unaffected | P0 |
| TC-SEC-048 | SC-SEC-048 | integration | Always-5xx endpoint | Send event; observe retries | Bounded jittered backoff; capped → DLQ; no storm | P0 |
| TC-SEC-049 | SC-SEC-049 | integration | Quota + anomaly alerting | Tenant exceeds quota / abnormal spike | Quota enforced; anomaly alert raised | P2 |
| TC-SEC-050 | SC-SEC-050 | security | Egress TLS | Subscriber w/ http / TLS1.0 / bad chain | http rejected; TLS<1.2 refused; cert chain validated | P0 |
| TC-SEC-051 | SC-SEC-051 | integration | Signed callback enabled | Receive outbound POST at test subscriber | Has HMAC sig + ts + id + `Idempotency-Key`; verifiable | P0 |
| TC-SEC-052 | SC-SEC-052 | integration | mTLS/OAuth2 sub | Configure `delivery.auth.strategy=bearer`/mTLS | Token w/ expiry / client cert presented on egress | P2 |
| TC-SEC-053 | SC-SEC-053 | integration | Audit sink | CRUD, secret-rotation, auth fail, delivery | Each emits tamper-evident audit record, tenant-scoped | P1 |
| TC-SEC-054 | SC-SEC-054 | security | Cookie-session Admin UI | Cross-site POST to sub CRUD without CSRF token | Rejected (CSRF/SameSite); token/HMAC API routes exempt | P1 |
| TC-SEC-055 | SC-SEC-055 | security | Existing safe sub | Update `callback_url` to `169.254.169.254` | Rejected at update; if persisted, blocked at delivery | P0 |

---

## 4. SUB — Subscription & routing tests

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-SUB-001 | SC-SUB-001 | integration | Auth tenant, connector exists | `POST /subscriptions` valid filter→target | 201, server id, row persisted, indexed | P1 |
| TC-SUB-002 | SC-SUB-003 | integration | Empty store | GET random/deleted id | 404, no other-tenant data | P1 |
| TC-SUB-003 | SC-SUB-004 | integration | 30 subs across 2 tenants | List as tenant A, page size 10 | Only tenant-A subs, paginated | P1 |
| TC-SUB-004 | SC-SUB-005 | integration | Sub exists | PATCH change CEL filter | Per model: 409 (immutable) OR atomic cut-over (mutable), no half-state | P2 |
| TC-SUB-005 | SC-SUB-007 | integration | Sub w/ queued deliveries, refcount=1 | DELETE sub | In-flight drained/cancelled, hook deregistered, row gone | P1 |
| TC-SUB-006 | SC-SUB-008 | unit | Sub already deleted | DELETE again | 200/204, no error, refcount not double-decremented | P2 |
| TC-SUB-007 | SC-SUB-009 | integration | Sub exists (sink+filter+tenant) | Re-POST identical | 200 existing id; single upstream hook (no 2nd register) | P2 |
| TC-SUB-008 | SC-SUB-010 | integration | None | Two POSTs same sink+filter | Two distinct ids OR hash-dedup per documented policy | P3 |
| TC-SUB-009 | SC-SUB-011 | integration | Batch of 3 (1 invalid CEL) | Bulk create | Valid persisted, invalid rejected per-item; no hook for failed item | P2 |
| TC-SUB-010 | SC-SUB-012 | integration | Connector w/ 1 sub | Concurrently subscribe + unsubscribe (threads) | Refcount consistent; no orphan hook, no lost update | P1 |
| TC-SUB-011 | SC-SUB-013 | integration | Tenant at quota limit | POST one more | 429; sub not persisted; no hook registered | P2 |
| TC-SUB-012 | SC-SUB-014 | integration | Sub `active=false` | Emit matching event | Matched, not delivered; deliveries metric=0 | P2 |
| TC-SUB-013 | SC-SUB-015 | e2e | Sub w/ dead callback | Emit events past failure threshold | Sub auto-disabled, marked unhealthy, alert/meter | P2 |
| TC-SUB-014 | SC-SUB-018 | unit | Filter refs renamed field | Eval CEL on new payload | `no_such_field` → no-match; worker survives | P1 |
| TC-SUB-015 | SC-SUB-019 | integration | Mutable filter, event stream live | Update filter mid-stream | Clean cut-over; each event eval against one version | P2 |
| TC-SUB-016 | SC-SUB-020 | integration | 3 subs match same event | Ingest 1 event | 3 deliveries, 3 independent tasks; isolation verified | P1 |
| TC-SUB-017 | SC-SUB-020 | integration | 3 subs, 1 sink hangs | Ingest 1 event | Other 2 delivered promptly; slow sub does not block | P1 |
| TC-SUB-018 | SC-SUB-021 | integration | No subs for connector | Create first sub | Exactly one upstream webhook registered via OAuth | P1 |
| TC-SUB-019 | SC-SUB-022 | integration | 1 sub exists | Create 2nd sub same connector source | Refcount=2, NO re-register (assert 1 provider call total) | P1 |
| TC-SUB-020 | SC-SUB-023 | integration | 1 sub, refcount=1 | Delete it | Refcount=0, upstream webhook deregistered | P1 |
| TC-SUB-021 | SC-SUB-024 | integration | Two subs same connector, refcount=2 | Delete one | Upstream hook retained, refcount=1, no deregister call | P1 |
| TC-SUB-022 | SC-SUB-025 | integration | Provider API 500 on register | Create sub | Atomic rollback OR pending state; NOT success; no dangling refcount | P1 |
| TC-SUB-023 | SC-SUB-026 | integration | OAuth token missing `admin:repo_hook` | Create sub | Actionable scope error; no sub, no refcount | P2 |
| TC-SUB-024 | SC-SUB-029 | integration | Valid sub, no matching events | Run idle window | No errors/leak; zero-delivery metric present | P3 |
| TC-SUB-025 | SC-SUB-030 | integration | Sub set not matching event | Ingest unmatched event | Dropped, debug log + unmatched meter increments | P3 |
| TC-SUB-026 | SC-SUB-031 | integration | Inject refcount drift (orphan hook) | Run reconciliation loop | Drift detected + repaired; leak metered | P2 |
| TC-SUB-027 | SC-SUB-032/033 | unit | CEL `data.amount > 100` | Eval on amount=150 then 50 | true→deliver; false→no-deliver | P1 |
| TC-SUB-028 | SC-SUB-034 | unit | Malformed expr `data.amount >` | POST sub | 422 reject at create; not persisted | P1 |
| TC-SUB-029 | SC-SUB-035 | unit | Valid-syntax but runtime-errs expr | Eval at delivery | Fail-closed no-match; metered; loop alive | P1 |
| TC-SUB-030 | SC-SUB-036 | unit | `has(data.x) && data.x=="y"`, x absent | Eval | No-match, no exception | P1 |
| TC-SUB-031 | SC-SUB-037 | unit | Expr forcing `no_matching_overload` | Eval | No-delivery, logged, no crash | P2 |
| TC-SUB-032 | SC-SUB-038 | unit | Over-budget/expensive expr | POST then eval | Rejected at create (static cost) OR halted at runtime (budget), metered | P2 |
| TC-SUB-033 | SC-SUB-039 | unit | Type-mismatch expr (`data.s > 1`) | POST sub | Compile-time rejection 422 | P2 |
| TC-SUB-034 | SC-SUB-040 | unit | `data.x` present-null vs absent | Eval both w/ `has()` | Distinguished correctly; no crash | P2 |
| TC-SUB-035 | SC-SUB-041 | unit | `data.a.b.c.d` w/ missing mid-level | Eval guarded vs unguarded | Guarded→no-match; unguarded depth>cap rejected at create | P3 |
| TC-SUB-036 | SC-SUB-042 | unit | `false&&bad`, `true||bad`, `cond?a:bad`, `exists` | Eval each | Short-circuit absorbs error per CEL semantics | P1 |
| TC-SUB-037 | SC-SUB-043 | unit | Type `com.stripe.payment_intent.succeeded` | Match `com.stripe.*`, `com.stripe.payment_intent.*`, `com.stripe` | Prefix anchored matches; bare prefix no-match; case-sensitive; no ReDoS | P1 |
| TC-SUB-038 | SC-SUB-045 | unit | Contradiction `data.x==1 && data.x==2` | POST sub | Warn (dead sub); persisted or rejected per policy | P3 |
| TC-SUB-039 | SC-SUB-046 | unit | Attr-only filter vs `data` filter | Eval fast-path then full CEL | `type` equality short-circuits; full CEL only when needed | P2 |
| TC-SUB-040 | SC-SUB-047 | integration | Callback needs handshake | Create sub w/ WebSub/Event Grid/CloudEvents endpoint | Handshake performed; fail→sub rejected | P1 |
| TC-SUB-041 | SC-SUB-048 | unit | `callback_url=169.254.169.254/...` | Create sub | SSRF guard blocks; 422 | P1 |
| TC-SUB-042 | SC-SUB-049 | integration | Callback 503 transiently | Deliver | Retried w/ backoff; succeeds on recovery | P2 |
| TC-SUB-043 | SC-SUB-050 | integration | Callback hard 403 (NO_PERMISSIONS) | Deliver | No futile retry; routed to dead_letters; metered | P2 |
| TC-SUB-044 | SC-SUB-051 | integration | DLQ sink also failing | Force DLQ write failure | Drop + log + alert; documented gap | P3 |
| TC-SUB-045 | SC-SUB-052 | integration | Ambiguous delivery (timeout then retry) | Deliver, force retry | Same `Idempotency-Key=delivery_id`; receiver dedupes | P1 |
| TC-SUB-046 | SC-SUB-053 | integration | Two subjects interleaved | Deliver burst | Per-subject order preserved per sub; no global-order asserted | P2 |
| TC-SUB-047 | SC-SUB-054 | integration | Correlate-mode reply | Emit reply event | Reply re-filtered, not blindly re-emitted (no loop) | P2 |
| TC-SUB-048 | SC-SUB-055 | integration | Waiting run w/ correlation_value=X | Ingest event correlation_value=X | Exact-match resume of that run; no fan-out | P1 |
| TC-SUB-049 | SC-SUB-056 | integration | No waiting run for value Y | Ingest event correlation_value=Y | Defined drop/park + meter; no crash | P2 |
| TC-SUB-050 | SC-SUB-057 | integration | spawn-mode sub, no waiter | Ingest matching event | New run started; does not block on absent waiter | P2 |

---

## 5. COR — Correlate-mode tests

> All COR wire-format assertions use the **tolerant parser** (accept field aliases `taskId`/`id`, `status`/`state`, unknown/extra fields); assert behavior, not exact wire names — `#523`/Tasks identifiers are provisional.

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-COR-001 | SC-COR-001 | integration | run waiting on taskId T1; correlate sub active | tools/call→task+202; poll→completed; fetch result | exact run T1 resumed; marked done; no new run | P0 |
| TC-COR-002 | SC-COR-001 | integration | as above | deliver completion via notifications/tasks/status instead of poll | same run resumed; identical outcome | P1 |
| TC-COR-003 | SC-COR-002 | integration | run waiting on T1 | task →failed; fetch result | same run resumed with failure; no new run | P0 |
| TC-COR-004 | SC-COR-003 | integration | run waiting, polling active | issue tasks/cancel→cancelled | run resumed cancelled; poll loop stopped | P1 |
| TC-COR-005 | SC-COR-004 | integration | run waiting | external cancel; poll observes cancelled | run resumed cancelled; polling stops | P1 |
| TC-COR-006 | SC-COR-005 | unit | task already terminal | tasks/cancel→ -32602 | error swallowed; read actual terminal result; resume | P1 |
| TC-COR-007 | SC-COR-006 | integration | run waiting; short TTL/watchdog | no terminal ever; let TTL/watchdog elapse | synthetic timeout; run resolved; polling stopped; alert; no hang | P0 |
| TC-COR-008 | SC-COR-007 | integration | correlate sub TTL expired; waiter purged | completion arrives after TTL expiry | no new run; routed to dead-letter+audit; late-pull persistence present | P0 |
| TC-COR-009 | SC-COR-008 | integration | task purged by receiver | tasks/get→ -32602 "expired" | resolved expired (distinct from never-existed); alert | P1 |
| TC-COR-010 | SC-COR-009 | integration | run waiting on T1 | deliver same completion twice (notif + webhook retry) | first resumes+consumes; second no-op (idempotent on taskId) | P0 |
| TC-COR-011 | SC-COR-010 (neg) | integration | no pending run for T9 | inbound completion for unknown T9 (valid sig) | rejected unmatched; no run created; logged + dead-lettered | P0 |
| TC-COR-012 | SC-COR-011 (neg) | unit | two concurrent calls minted same key (forced) | register colliding correlation_value | collision detected; second fails-closed; keys globally unique | P0 |
| TC-COR-013 | SC-COR-012 (neg) | integration | run waiting under team A; taskId from team B | completion for foreign-context taskId | rejected (auth-context binding); no resume; logged | P0 |
| TC-COR-014 | SC-COR-013 | integration | T1 completion arrives; run record deleted/crashed | deliver completion | no respawn; idempotent deliver if resolvable else dead-letter; audit | P1 |
| TC-COR-015 | SC-COR-014 | unit | run saw completed(lastUpdatedAt=t2) | deliver stale working(t1<t2) | regression ignored; state stays completed | P1 |
| TC-COR-016 | SC-COR-015 (neg) | unit | task terminal=completed | receive terminal→working | regression rejected; terminal sticky; violation logged | P1 |
| TC-COR-017 | SC-COR-016 | integration | run waiting on T1 | receive notifications/progress + interim working | forwarded as progress; correlation NOT consumed/resumed | P1 |
| TC-COR-018 | SC-COR-017 | integration | run waiting on T1 | task→input_required | surfaced; InputRequiredResult+state token; correlation kept alive; later completion resumes | P1 |
| TC-COR-019 | SC-COR-018 | integration | receiver never sends status notifications | only poll loop, respecting pollInterval | completion detected via tasks/get; run resumed | P0 |
| TC-COR-020 | SC-COR-019 | integration | #523 self-webhook target down | webhook never arrives | poll fallback detects terminal; downstream callback queued+retried, dead-lettered if exhausted | P1 |
| TC-COR-021 | SC-COR-020 (neg) | integration | inbound #523 completion w/ bad/forged sig | POST completion to ingress | 401/403; nothing resumed; logged; no run touched | P0 |
| TC-COR-022 | SC-COR-021 | integration | correlate sub TTL < completion time | let TTL expire then deliver completion | sub expired; run resolved timed-out; later completion rejected | P0 |
| TC-COR-023 | SC-COR-022 | integration | many pending tasks; receiver 429 | run poll scheduler | pollInterval honored; jittered backoff; concurrent polls capped; per-taskId coalesced | P1 |
| TC-COR-024 | SC-COR-023 | unit | tasks/get returns -32603/network error | poll T1 | retried w/ backoff; run kept pending; resolved only on terminal or run timeout | P1 |
| TC-COR-025 | SC-COR-024 | integration | normal tools/call (non-task) | server responds with CreateTaskResult shape | shape detected on any call; correlation opened; switched async (tolerant parser) | P0 |
| TC-COR-026 | SC-COR-025 | integration | pending-run↔taskId map persisted; restart | restart; no tasks/list | known taskIds re-polled individually; pending runs recovered | P0 |
| TC-COR-027 | SC-COR-026 | integration | result fetched; resume crashes pre-commit | re-drive resume after crash | persisted result reused; resume idempotent; no re-fetch of purged task; resumed once | P1 |
| TC-COR-028 | SC-COR-027 | integration | notification + poll both observe terminal | trigger both near-simultaneously | single-flight per taskId; one tasks/result fetch + one resume | P0 |
| TC-COR-029 | SC-COR-009/027 | unit | idempotency keyed on taskId | replay N identical terminal events | exactly one consume; N-1 no-ops; Idempotency-Key honored | P1 |
| TC-COR-030 | tolerant parser | unit | provisional identifiers (#523/Tasks) | parse CreateTaskResult/status w/ renamed/extra fields | accepts variants (taskId vs id, status aliases); no hard failure | P1 |

---

## 6. MCP — MCP-native notification tests

| TC ID | Covers | Level | Preconditions | Steps | Expected result | Pri |
|-------|--------|-------|---------------|-------|-----------------|-----|
| TC-MCP-001 | SC-MCP-001 | integration | Persistent session, 1 subscriber on uri X | Mock upstream emits `resources/updated {uri:X}` | Synthesize id, normalize (subject=X), issue `resources/read`, XADD one event | P0 |
| TC-MCP-002 | SC-MCP-002 | unit | Notification uri only, no id | Pass payload to id synthesizer twice (same server+uri+seq) | Stable non-null id; deterministic; differs for different uri/seq | P0 |
| TC-MCP-003 | SC-MCP-003 | integration | Subscriber on X, dedup TTL set | Deliver same updated twice (Last-Event-ID replay), identical synthesized id | Second dedup-dropped; one downstream emit; dedup metric increments | P0 |
| TC-MCP-004 | SC-MCP-004 | e2e | Persistent session + 3 active resource subs | Force upstream 404 on `Mcp-Session-Id`; trigger reconnect | Re-init, re-list, re-issue all 3 `resources/subscribe`; restored; no silent drop | P0 |
| TC-MCP-005 | SC-MCP-005 | integration | Valid session, broken stream | Reconnect with Last-Event-ID; verify no full reinit | Same stream resumed; no cross-stream replay accepted; replays deduped; no resubscribe | P1 |
| TC-MCP-006 | SC-MCP-006 | integration | Sub on X; disconnect; change X upstream while down | Reconnect + resubscribe; observe reconcile | `resources/read` X + re-list; emit synthetic "may-have-missed"; no silent drop | P1 |
| TC-MCP-007 | SC-MCP-007 | unit | Caps: `resources.subscribe=false` | Attempt to enable live updates | No `resources/subscribe` call; polling/listChanged fallback; "no live updates" surfaced | P1 |
| TC-MCP-008 | SC-MCP-008 | integration | Caps advertise subscribe but server method-not-found | Issue `resources/subscribe` | Caught; degrade to polling; mismatch recorded; session alive | P1 |
| TC-MCP-009 | SC-MCP-009 | integration | Subscribe to bogus uri | `resources/subscribe` on missing uri → -32002 | Error surfaced; no refcount/phantom sub | P3 |
| TC-MCP-010 | SC-MCP-010 | integration | Cached tool list present | Emit `tools/list_changed` | Cache invalidated; `tools/list` re-fetched; tools re-exposed | P1 |
| TC-MCP-011 | SC-MCP-010 | integration | Reconnect path after session 404 | Reconnect; assert tools re-queried | `tools/list` re-issued on reconnect (regression guard) | P1 |
| TC-MCP-012 | SC-MCP-011 | integration | Subs on [A,B]; B removed upstream | Emit `resources/list_changed` | `resources/list` re-fetched; B's sub dropped; A retained; new uris per policy | P1 |
| TC-MCP-013 | SC-MCP-012 | integration | Active sub on X | Signal X removed (via list_changed) | "resource gone" emitted; sub + refcount torn down; no further read on X | P1 |
| TC-MCP-014 | SC-MCP-013 | unit | uri Y refcount 0 | Receive updated for Y w/ no subscriber | No upstream subscribe; accept-and-drop; `resources/unsubscribe` when last leaves | P3 |
| TC-MCP-015 | SC-MCP-014 | integration | Sub on X; debounce window=N ms | Emit 10 updated for X within window | Coalesced to one `resources/read` after quiescence; fan-out rate-limited | P1 |
| TC-MCP-016 | SC-MCP-015 | integration | Two rapid updated for X | Deliver update#1 then #2; intercept reads | Last-read-wins authoritative state; read-after-notify; no stale content | P1 |
| TC-MCP-017 | SC-MCP-016 | e2e | Long-lived session, OAuth near expiry | Let token expire mid-stream → 401 | 401 detected; token refreshed; session+subs re-established; "auth-required" only if refresh fails | P1 |
| TC-MCP-018 | SC-MCP-017 | integration | logging routed to sink | Flood `notifications/message` | Routed to log sink at negotiated level; not triggers; rate-limited | P3 |
| TC-MCP-019 | SC-MCP-018 | unit | Server emits no logs pre-setLevel | Connect with logging desired | `logging/setLevel` sent on connect; early logs tolerated | P3 |
| TC-MCP-020 | SC-MCP-019 | integration | Sub on X; upstream read fails (-32603/timeout) | Notify updated; force `resources/read` failure | Backoff retry; on gone emit "update detected, content unavailable"; not dropped; stream not blocked | P1 |
| TC-MCP-021 | SC-MCP-020 | integration | 3 downstream subscribers same uri X | Each subscribes; one updated arrives | One upstream `resources/subscribe`; fan-out to 3; unsubscribe after 3rd leaves | P1 |
| TC-MCP-022 | SC-MCP-021 | e2e | 2 workers, streamable-HTTP session on worker A | Route follow-up to worker B → 404 | Sticky/shared-store resolves, or re-init+resubscribe on B; no orphaned subs | P0 |
| TC-MCP-023 | SC-MCP-022 | integration | Cached prompts present | Emit `prompts/list_changed` | Prompts cache invalidated; `prompts/list` re-fetched | P3 |
| TC-MCP-024 | SC-MCP-023 | unit | Tracking subs [X]; updated for unknown Z | Deliver updated{uri:Z} | Ignored/logged; no fan-out; no auto-subscribe to Z | P3 |
| TC-MCP-025 | SC-MCP-001/004 (regression) | integration | Current one-shot `async with ClientSession` (gateway_service.py) | Subscribe then close `list_*` block | Guard: persistent-session mode keeps session open post-list, retains subs (no teardown-drop) | P0 |

---

## 7. Traceability

- **Requirement → Scenario:** each scenario row's `FRD refs` column (in the scenario catalog) links back to `frd-mcp-triggers-events.md` `FR-*`/`NFR-*`/`R-*`. Scenarios marked `—, propose FR` map to the proposed requirements in scenarios §7.
- **Scenario → Test:** each test row's `Covers` column links to one or more `SC-*` ids. Every scenario has ≥1 test; Critical/Major scenarios have a positive and at least one negative/boundary test.
- **Coverage rule:** a milestone is "test-complete" when every P0/P1 test for its gating categories passes and every Critical scenario in those categories has a passing negative test.

## 8. Milestone gating

| Milestone | Gating test categories | Must-pass (examples) |
|-----------|------------------------|----------------------|
| **M1** Config-driven ingress + verify + envelope | ING; SEC (signature/replay/verify-before-side-effect subset) | TC-ING-001/002/017/018/020/021/043/056/062/063/067/068; TC-SEC-001/003/005/006/008/009/011 |
| **M2** L2 bus + delivery worker + `/subscriptions` API | DEL (bus/reliability); SUB (lifecycle/CEL/fan-out) | TC-DEL-001/004/009/020/021/026/027; TC-SUB-001/010/016/019/020/021/022/028/029 |
| **M3** Egress adapter (HTTP-callback + SSE/WS) | DEL (delivery); SEC (egress SSRF/TLS/signing) | TC-DEL-010/011/016/017/018/029/037; TC-SEC-020/021/022/023/024/025/050/051/055 |
| **M4** Egress → budprompt | COR (correlate subset for agent invoke); SEC (tenant isolation) | TC-SEC-028/029; TC-SUB-048; delivery-shape tests against budprompt receiver |
| **M5** Egress → bda | DEL (delivery); end-to-end callback to bda-reactive | TC-DEL-002/015; e2e callback-to-bda |
| **M6** MCP-native adapter | MCP (all) | TC-MCP-001/002/003/004/022/025 |
| **M7** Correlate mode + per-op webhooks (#523) | COR (all) | TC-COR-001/007/008/010/011/012/013/021/025/026/028 |
| **M8** budpipeline multi-step (optional) | SUB (correlate-to-workflow); DEL (ordering) | TC-SUB-048/049; TC-DEL-014 |

Cross-cutting SEC tests (rate-limit/quota, audit, secret rotation, DoS limits, log-injection, stored-XSS) run against every milestone that introduces the relevant surface; the security suite is a release gate independent of milestone.
