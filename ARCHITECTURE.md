# Page Pulse at scale — architecture document

Task B: scaling the URL-audit service to 10,000 audits/day, bursts of 500
concurrent requests, with a customer-facing response-time SLA.

The Task A service (single process, in-memory cache and rate limiter) works
fine at low volume but has two hard ceilings at this scale: it can't survive
a burst of 500 concurrent outbound audits without exhausting connections,
and its in-memory state doesn't scale across multiple instances or survive
a restart. Everything below addresses those two ceilings.

## a) Components, data flow, queueing strategy, and where state lives

**Components**
- **Load balancer** — distributes incoming requests across API instances,
  health-checks them, and gives us a single point to horizontally scale
  behind.
- **API instances (stateless, auto-scaled)** — handle request validation,
  check the cache/rate limiter, and either return a cached result
  immediately or enqueue a new audit job. They hold no local state, so any
  instance can serve any request.
- **Redis (cache + rate limiter)** — shared state layer. Holds cached audit
  results (keyed by URL, TTL-based) and per-client rate-limit counters
  (token bucket, same algorithm as Task A, just backed by Redis `INCR` +
  `EXPIRE` instead of an in-process dict). Because it's shared, rate limits
  and cache hits are consistent no matter which API instance handles a
  request.
- **Job queue** — decouples "accept the request" from "do the audit". A
  burst of 500 requests lands in the queue almost instantly; workers drain
  it at a controlled rate instead of firing 500 simultaneous outbound HTTP
  calls.
- **Worker pool (auto-scaled on queue depth)** — pulls jobs, performs the
  actual audit fetch against the target URL, writes the result to Redis
  (for future cache hits) and Postgres (for history/audit trail), and marks
  the job complete.
- **Results store (Postgres)** — durable history of audits: useful for
  analytics, debugging, and satisfying any "show me past audits" feature
  later. Redis is the fast path for cache hits; Postgres is the durable
  system of record.

**Data flow**
1. Client sends `POST /audit` → load balancer → an API instance.
2. API instance checks Redis for a cached result. Cache hit → return
   immediately, no queueing needed.
3. Cache miss → API instance checks the Redis-backed rate limiter for that
   client. Over limit → `429` immediately.
4. Under limit → API instance pushes an audit job onto the queue and
   returns `202 Accepted` with a job ID (see below on why this becomes
   async at this scale).
5. A worker picks up the job, fetches the target URL, writes the result to
   Redis (cache) and Postgres (history).
6. Client either polls `GET /audit/{job_id}` or, for a snappier UX, holds a
   WebSocket/long-poll connection that the worker notifies on completion.

**Where state lives:** all shared state (cache, rate-limit counters, queue)
lives in Redis; all durable history lives in Postgres. Nothing meaningful
lives in the memory of any individual API instance or worker — that's what
makes it safe to scale either tier horizontally or lose an instance without
losing data.

## b) Technology choices and rejected alternatives

| Choice | Why | Rejected alternative | Why not |
|---|---|---|---|
| **Redis** for cache + rate limiting | Sub-millisecond reads, atomic `INCR`/`EXPIRE` make the token-bucket rate limiter trivial and race-free across instances, and it's the de facto standard for this exact job | In-process cache (Task A's approach) | Doesn't survive a restart, and each instance would have its own view of the rate limit — a client could get 30 req/min per instance instead of 30 req/min total |
| **Message queue** (e.g. SQS/RabbitMQ/Redis Streams) for job handoff | Absorbs bursts by letting accept-rate and process-rate differ; workers can be scaled independently of API instances | Handling the audit synchronously inside the request (Task A's approach) | A 500-request burst would mean 500 simultaneous outbound HTTP calls from the API layer itself — that's how you exhaust file descriptors and outbound connections and take the whole service down, not just slow it |
| **Async job + poll/notify pattern** for the client contract | Keeps API response times fast and predictable (bounded by "accept the job," not "wait for a slow target site") | Keeping `POST /audit` synchronous and just scaling harder | The SLA is about *our* response time — a target site that takes 8 seconds to respond would blow any synchronous SLA no matter how many workers we add. Decoupling accept-time from audit-time is what actually protects the SLA |
| **Postgres** for durable history | Relational queries over audit history (by client, by date, by status) are a natural fit, and it's a well-understood operational surface | Keeping everything in Redis only | Redis is not the right tool for durable, queryable history — it's optimized for speed, not for being the system of record |
| **Auto-scaling workers on queue depth** | Directly ties capacity to actual backlog rather than guessing at request rate | Fixed worker pool sized for peak | Wastes money at normal load (10k/day is not evenly distributed) and still risks falling behind during an unexpected spike above the fixed size |

## c) Three most likely failure modes and mitigations

1. **Queue backlog growing faster than workers can drain it** (e.g. a spike
   well above the expected 500-burst, or many audited URLs being slow).
   *Mitigation:* auto-scale workers on queue depth with a defined ceiling;
   set a max queue size and return `503`/"try again shortly" once it's
   exceeded rather than accepting unbounded backlog; alert when queue depth
   or oldest-job-age crosses a threshold so it's caught before the SLA
   breaches.

2. **A slow or hanging target site cascades into worker exhaustion** — if
   many queued jobs are audits of a slow site, workers can all end up
   blocked waiting on that one site, starving audits of unrelated URLs.
   *Mitigation:* keep the per-request timeout (from Task A) enforced at the
   worker level too; cap concurrent in-flight audits per target host, not
   just globally, so one bad site can't monopolize the whole worker pool.

3. **Redis becomes a single point of failure** — since both the cache and
   the rate limiter depend on it, a Redis outage takes down request
   handling entirely, not just degrades it.
   *Mitigation:* run Redis in a managed, replicated configuration (e.g.
   primary + replica with automatic failover) rather than a single node;
   design the API layer to fail *open* on rate-limit-check errors (allow
   the request through rather than reject) but fail *closed* on cache
   writes (just skip caching) so a Redis blip degrades performance, not
   availability.

## d) Monitoring, alerting, and rollback

**What to monitor**
- API layer: request rate, p50/p95/p99 latency, error rate by status code
- Queue: depth, oldest-job age, enqueue vs. drain rate
- Workers: in-flight audit count, per-target-host concurrency, audit
  success/failure rate, audit duration distribution
- Redis: memory usage, hit/miss ratio (cache effectiveness), command
  latency
- Postgres: write latency, connection pool saturation

**What to alert on**
- p95 API response time approaching the SLA threshold
- Queue oldest-job-age exceeding a set bound (signals we're falling behind
  real-time)
- Error rate (5xx) above a small baseline percentage
- Redis or Postgres availability/replication lag

**Rollback plan**
- Deploy via blue-green or canary: route a small percentage of traffic to
  the new version first, watch the error-rate and latency dashboards for a
  fixed window, then shift full traffic over.
- Keep the previous version's containers/instances warm (not torn down)
  until the new version has been stable in production for a defined period,
  so rollback is "flip traffic back," not "rebuild from scratch."
- Any deploy that trips the alert thresholds above during its canary window
  auto-rolls-back rather than waiting for a human to notice.

---

Built for Digital Heroes Training Task — [digitalheroesco.com](https://digitalheroesco.com)
