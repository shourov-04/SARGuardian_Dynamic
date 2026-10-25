# Displacement Actions integration

`Find Displacements` creates an authenticated, durable `displacement_jobs` record and dispatches `.github/workflows/sarguardian-displacement.yml`. The browser never receives GitHub or callback secrets.

## Required server environment variables

- `DISPLACEMENT_ACTIONS_ENABLED=true`
- `GITHUB_ACTIONS_TOKEN`: fine-grained token limited to **Actions: write** for `GITHUB_ACTIONS_REPOSITORY`.
- `GITHUB_ACTIONS_REPOSITORY`: `owner/repository` containing this workflow.
- `GITHUB_ACTIONS_WORKFLOW=sarguardian-displacement.yml`
- `GITHUB_ACTIONS_REF=main`
- `DISPLACEMENT_CALLBACK_SECRET`: shared HMAC secret; never expose as `VITE_*`.
- `PUBLIC_API_URL`: deployed HTTPS API origin, without a trailing slash.

The existing `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, Firebase service-account configuration and restrictive `CORS_ORIGINS` remain required.

## Required GitHub Actions secrets

- `DISPLACEMENT_CALLBACK_SECRET`: exactly the backend value.
- `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD`: a valid Earthdata Login account with
  access to the ASF/NISAR archive. These values are available only to the runner and
  are used by the pinned source downloader; they must never be returned to the browser.
- `GOOGLE_DRIVE_OAUTH_CLIENT_JSON`: a Google OAuth **web client** credential for the
  dedicated SARGuardian storage account. Do not use a Firebase or service-account
  credential for this value.
- `GOOGLE_DRIVE_REFRESH_TOKEN`: offline OAuth refresh token granted by that storage
  account with Drive upload access.
- `GOOGLE_DRIVE_FOLDER_ID`: the parent folder for per-job result folders.

The account-facing workflow calls the vendored dynamic-AOI streaming pipeline from
`moshiour0/SARGuardian-backend`. It pins `moshiour0/SARGuardian` at
`10639a916c9829850775410b3ed7de62e527770d`, uses the submitted WKT polygon unchanged,
queries ASF separately by family and BETA/PROVISIONAL maturity, and retains
catalog/identity evidence. The default policy selects pairs by an in-window **secondary
acquisition** date; an optional strict mode requires both dates in the window. Products
run sequentially, with one raw download per execution and at most one receipt-gated
product retry.

Each product is validated against the requested AOI, processed by the original pinned
GUNW/GOFF reader, uploaded to private Drive, and remotely verified before local raw data
can be deleted. The dashboard job then retains the derived-results ZIP, run summary and
product table. The API verifies Firebase ownership and streams a requested artifact only
after rechecking its retained SHA-256 and byte count. No public Drive permission is
created. `derived-only` never retains raw NISAR HDF5; `full-lossless` retains the
lossless curated archive. Missing Drive credentials or any failed remote verification
prevents a success callback.

Before reporting success, the wrapper cross-checks the selected-product manifest,
processing report, PASS run summary, checksummed private Drive receipt, submitted request
fingerprint and exact science/orchestration commits. A zero process exit code is
insufficient. The supported result is AOI-clipped, per-product GUNW/GOFF displacement
screening; it is not a common-reference time series or hazard forecast. Callbacks have
signed, ordered sequence numbers and bind the job ID plus request fingerprint. GitHub
terminal success without the signed completion callback is recorded as failure.

Run [002_displacement_jobs.sql](../Backend/migrations/002_displacement_jobs.sql) after the prior processing-job migration. Keep direct browser access revoked; the trusted backend uses the Supabase service-role key and verifies Firebase ownership before reads or writes.
