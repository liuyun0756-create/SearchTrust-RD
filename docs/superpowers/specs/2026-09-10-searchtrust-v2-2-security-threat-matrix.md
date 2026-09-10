# SearchTrust V2.2 security threat-control-test matrix

Date: 2026-09-10

Status: implementation evidence index for V22-081. Production Google OAuth,
Google sync and Verified Generation remain disabled.

| Threat | Control | Automated evidence | State |
|---|---|---|---|
| OAuth CSRF, callback substitution and replay | Signed one-time state, PKCE S256, login-cookie binding, expiring/consumed OAuth session | `search-trust/src/lib/google-connections/oauth-state.test.ts`, OAuth handler/service suites | Existing control verified |
| Scope escalation or wrong Google identity | Fixed source scope catalog, exact subject/resource binding, incremental scope validation | `search-trust/src/lib/google-connections/service.test.ts`, `google-resources/identity.test.ts` | Existing control verified |
| Broker forgery, replay, expiry or cross-source use | HMAC request signature, bounded time window, nonce/request claim, source scope check, one-time persistence | `broker-signature.test.ts`, `broker.test.ts`, `handlers.test.ts` | Existing control verified |
| Ciphertext copied across user, connection, field or environment | AES-256-GCM with versioned key and contextual AAD | `token-vault.test.ts` | Existing control verified |
| Rotation interruption, stale overwrite or old-key removal too early | Dual-key active version, small batches, fresh IV, compare-and-swap, resumable execute and old-count-zero verify gate | `rotation.test.ts`; `security:rotate-google-tokens` dry-run/execute/verify | Added in V22-081 |
| Direct private URL, mixed DNS, mapped IPv6, metadata host, DNS rebinding or redirect to private network | One shared URL validator plus pinned-IP transport; validate every redirect; response/redirect/time bounds | `SearchTrust-RD/tests/test_v22_preflight_urls.py`, `test_v22_security_url_boundary.py`, fetcher/site-inventory suites | Added and unified in V22-081 |
| Anonymous share guessing, cross-report access, expiry/revocation bypass | 256-bit token digest lookup, one active share, fixed 30-day expiry, shared resolver, client-only view and uniform 404 | `report-shares/service.test.ts`, `v22Migration.test.ts`, `headers.test.ts` | Reinforced in V22-081 |
| Share bearer credential captured in hosting or analytics logs | Token in URL fragment only, fixed `/share` request path, bounded POST-body exchange, no analytics initialization on share page, no-referrer/no-store | `report-shares/service.test.ts`, `public-request.test.ts`, `security-v22/browser-analytics.test.ts`, `headers.test.ts`, production artifact/log scan | Added in V22-081 |
| OAuth token, API key, Cookie, provider body, email or URL leaks in logs/errors/bundles | Shared allowlist logger, fixed public errors, output length bounds, sentinel artifact scanner | `security-v22/safe-log.test.ts`, `artifact-scan.test.ts`, `SearchTrust-RD/tests/test_v22_security_logging.py`, `security:scan` | Added in V22-081 |
| Deletion leaves usable broker token or user graph | Prepare fence changes connections to `deleting`; bounded provider revoke; atomic receipt plus user delete cascades graph | `identity-webhooks/service.test.ts`, `handler.test.ts`, `v22Migration.test.ts` | Added in V22-081 |
| Duplicate or out-of-order Clerk events restore or delete wrong identity | Verified Svix signature, Clerk subject as sole lookup key, 90-day digested receipt, idempotent completion and create guard | `identity-webhooks/handler.test.ts`, `service.test.ts`, `v22Migration.test.ts` | Added in V22-081 |
| Provider outage prevents privacy deletion | Provider revoke is bounded and best-effort; local credential and graph deletion remains authoritative | `identity-webhooks/service.test.ts` | Added in V22-081 |

## Fixed security result contracts

- Public share failures: `404 Report share not found` or fixed generic server error.
- Clerk signature failure: `401 WEBHOOK_SIGNATURE_INVALID`; malformed event: fixed `400` code.
- Local identity deletion transaction failure: retryable fixed `5xx` response; provider
  revoke details never cross the route boundary.
- Rotation CLI: `ROTATION_FAILED` plus secret-free counters only.
- URL security rejection: stable application security error categories; resolved addresses,
  request bodies and provider responses are not returned to users.

## Release evidence still required

- Complete frontend and backend regression gates.
- Production migration-before-code ordering.
- Isolated synthetic rotation, share revoke and signed delete drill, followed by cleanup.
- Production log/artifact scan and explicit confirmation that every Google/Verified flag is off.
