# Security policy

## Reporting a vulnerability
Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include steps to reproduce and the affected
version or commit.

## Security model

**Credentials**
- First run: no credentials exist and every protected route is denied. The server prints a one-time setup code to
  its log; whoever presents it in the browser sets the admin password. The code is held in memory only and changes
  on each restart until setup is complete.
- The admin password is stored only as a salted scrypt hash (N=2^15, r=8, p=1), 12-character minimum.
- Browser sessions use a random cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS); only its SHA-256 is
  stored. Sessions last 12 hours and all end when the password changes or you sign out. Browser writes also need a
  custom header, and no credential is kept in page storage.
- API tokens are random, shown once, stored as SHA-256 hashes, revocable, and have exactly one of four scopes:
  `ingest` (push readings; reads nothing), `read` (dashboard data; changes nothing), `metrics` (only `GET /metrics`)
  and `full` (everything an admin session can do, including starting bursts and managing tokens). Scopes are strict:
  a non-`full` token must match the route's scope exactly.
- With no credential configured the app **denies everything**. Anonymous access exists only behind the explicit
  opt-in `CITYPULSE_ALLOW_ANONYMOUS=true`; never use it on a reachable host.
- Failed credentials are rate limited (10 per minute per client address, in memory, per process). Behind a reverse
  proxy all clients can share one address, so add proxy-level limits too.

**Collection paths**
- `/api/readings` and `/v1/metrics` need an `ingest` token. Every item is validated (sensor id, known type,
  district, finite number within bounds, timestamp window of 24 hours back and 2 minutes ahead). Request bodies are
  capped at 2 MB, batches at 1000 readings, gzip bodies are inflated with a hard 8 MB limit, and unknown encodings
  and content types are refused. The number of auto-registered sensors is capped (`MAX_SENSORS`).
- The gateway never blocks without bound: when its write slots or database connections are exhausted it answers
  `429` with `Retry-After`, so a flood becomes back-pressure on the sender instead of memory growth here.
- `/metrics` needs a `metrics` token and uses route templates, never raw paths, as labels.
- Burst controls need a `full` credential. Bursts only change the *simulated* fleet and the gateway's own delay;
  readings from real sensors are never altered. Duration is capped at 30 minutes and active bursts at 50.

**Outbound requests**
- The optional PagerLens reporter sends one token to one configured URL, never follows redirects, refuses plain
  `http` unless the host is localhost, and never logs the token.
- The simulator's HTTP sink sends its ingest token only to `CITYPULSE_INGEST_URL` and does not follow redirects.

**Container and database**
- The image runs as a non-root user, holds no secrets, and takes its connection details at runtime.
- All SQL is parameterised. Sensor search uses `position()` rather than `LIKE`, so user input has no wildcard
  semantics. The interface sets a strict Content-Security-Policy (no inline scripts or styles), `X-Frame-Options:
  DENY`, `nosniff` and `no-referrer`, and builds every DOM node with `textContent`.

**Known limits**
- Rate limiting and the token cache are per process.
- A leaked `ingest` token lets an attacker push readings (bounded by the validation above) until it is revoked;
  use one token per collector so you can revoke narrowly.
