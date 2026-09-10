# V22-090 Frontend test infrastructure completion

Date: 2026-09-10  
Status: completed and verified in production

## Outcome

SearchTrust V2.2 now has a production-blocking frontend quality gate. Fast Vitest
coverage remains the contract and state-machine foundation, Testing Library exercises
critical controls through accessible user interactions, and Playwright covers the six
approved end-to-end journeys against deterministic local fixtures.

The browser suite cannot contact live payment, identity, database, Railway, analytics,
Google or other provider services. Its fixed signed-in identity is accepted only on a
loopback host, is rejected whenever `VERCEL_ENV` is present, and all unexpected API
paths or external origins fail closed.

## Delivered coverage

- Prospect acquisition, including the blocking zero-competitor state and the approved
  minimum of one confirmed competitor.
- Case-scoped checkout success, cancellation and provider failure without a real charge.
- Prospect analysis with a deliberately interrupted stream, polling recovery and exactly
  three final actions.
- Synthetic Google authorization success, user denial and resource identity mismatch.
- Healthy Verified Core sources and the Verified report change explanation.
- Fragment-only share creation, public resolution and revocation with PostHog absent.
- Critical component busy, retry, accessibility, identity, sync and revocation states.

During implementation, the browser tests found and fixed two real boundary defects:
payment-return navigation was losing the saved test state, and the public share client
bundle imported Node's crypto module. Share token format validation is now browser-safe
while random generation and hashing remain server-only. Connection Center dates also use
a deterministic timezone so server and client rendering do not disagree.

## Verification evidence

- `npm run quality`: passed.
- TypeScript: passed.
- Vitest: 638 tests passed across 85 files.
- Component interaction suite: 9 tests passed across 6 files.
- Contract generation consistency: passed with no diff.
- Production Next.js build: passed; 35 static pages generated.
- Security scan: passed; 43 generated static files scanned in the production-equivalent run.
- Security negative control: a deliberate sentinel failed with only `[REDACTED]` reported,
  and passed again after cleanup.
- Playwright: 11 tests passed serially, with 9 local workers, and with the CI configuration
  of 2 workers.
- Browser external-request guard: no external calls or allowlist exceptions observed.
- Checkout and Google fixtures created no production payment, Case, Google resource or
  provider records.

Successful browser runs upload no diagnostics. On a final failure, only files copied to
`output/playwright/prepared` are eligible for upload, each file is limited to 25 MiB,
the set is limited to 50 MiB, unsafe files are excluded, and GitHub retention is seven
days. Zip traces are opened and scanned internally rather than trusted as opaque files.

## Deployment evidence

- Frontend gate commit: `df7a000` (`ci(v2.2): gate Vercel on frontend quality`).
- Automatic-deployment guard commit: `9e82d4f`
  (`fix(ci): prevent Vercel Git from bypassing gate`).
- Final GitHub Actions run: `34481203591` (`#27`), completed successfully in 1m 50s.
- Quality prerequisite: passed in 1m 11s, including 638 Vitest tests across 85 files
  and 22 workflow/configuration assertions.
- Browser prerequisite: passed in 1m 36s with all 11 deterministic local journeys.
- Deploy job: passed in 6s and ran only after both prerequisites succeeded.
- Vercel deployment: `dpl_A3r6t4vvUNynS2WaCN4NPD3Mo9E3`, production status `Ready`.
- Production alias: `https://trysearchtrust.com`, resolved to the new deployment and
  returned HTTP 200; no error-level runtime logs were observed in the first five minutes.

The first online validation run, `34469981110`, proved that the two prerequisite jobs
were green but exposed two release-configuration defects: the stored deploy-hook secret
was stale, and Vercel's connected-Git deployment could publish before the gate completed.
The deploy hook was replaced, the encrypted GitHub secret was updated, and
`git.deploymentEnabled` was set to `false`. A push of `9e82d4f` created no immediate
Vercel deployment; run `34481203591` created the new production deployment only after
the quality and browser jobs had passed.

## Unchanged boundaries

- Railway deployment automation is not part of this milestone and remains assigned to
  V22-091 together with queue/restart integration coverage.
- No Google production feature flag was enabled.
- Verified Generation remains closed until its already-defined release gate is met.
- No general-purpose CI or deployment credential was added. The branch-scoped deploy
  hook is stored as the encrypted `VERCEL_HOOK_URL` repository secret and is referenced
  only through a masked environment variable.

V22-090 is fully closed. Its deterministic local suite, online prerequisite jobs,
post-gate Vercel deployment and production alias have all been verified.
