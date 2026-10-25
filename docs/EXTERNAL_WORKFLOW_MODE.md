# Run the unchanged upstream workflow from the website

`DISPLACEMENT_WORKFLOW_MODE=external` lets the website drive the existing **SARGuardian AOI WKT E2E + Dashboard**
workflow (`sarguardian-aoi-wkt-dashboard.yml`) that lives in another GitHub account's repository
(for example `moshiour0/SARGuardian-backend`). That workflow is **not modified** and nothing is copied into this repository.

```
website  ->  account API  --workflow_dispatch-->  moshiour0/SARGuardian-backend  (runs there, with its own secrets)
   ^             |  polls the run (GitHub API)            |
   |             |  reads aoi-processing-report.json      v
   +-------------+<----- sarguardian-aoi-dashboard-<run>-<attempt> artifact
```

* **Input:** the AOI polygon, dates, acquisition window, retention and profile chosen on the website are sent as the
  workflow's six `workflow_dispatch` inputs (`aoi_wkt`, `start_date`, `end_date`, `date_filter_mode`, `run_profile`,
  `retention_profile`). `full-aoi` is sent as `full-aoi-baseline`.
* **Output:** the API follows the run, then reads only the small report inside the dashboard artifact (HTTP range
  requests, so a huge artifact is never downloaded by the API). The website shows status, product counts, a product table
  and a **Download results (ZIP)** button; the browser downloads the ZIP directly from GitHub through a short-lived signed
  link that only the job's owner can request.
* The result is accepted only if the report says `PASS` and describes the same AOI and dates that were submitted.

## One-time setup

1. **Token** — in the account that owns the workflow repository, create a *fine-grained personal access token*:
   Resource owner = that account, Repository access = *only* that repository, Permissions = **Actions: Read and write**
   (nothing else). The token must come from the repository owner; it is only used by the API and never reaches the browser.
2. **API environment** (Vercel project of `Backend/`, then redeploy):

   | Variable | Value |
   |---|---|
   | `DISPLACEMENT_WORKFLOW_MODE` | `external` |
   | `DISPLACEMENT_ACTIONS_ENABLED` | `true` |
   | `GITHUB_ACTIONS_REPOSITORY` | `moshiour0/SARGuardian-backend` |
   | `GITHUB_ACTIONS_WORKFLOW` | `sarguardian-aoi-wkt-dashboard.yml` |
   | `GITHUB_ACTIONS_REF` | `main` |
   | `GITHUB_ACTIONS_TOKEN` | the token from step 1 |

   `DISPLACEMENT_CALLBACK_SECRET` and `PUBLIC_API_URL` are not needed. No database migration is needed.
3. Submit an AOI on the website. A `workflow_dispatch` run should appear in that repository's **Actions** tab, and the
   website's task page shows a *GitHub run* link, the stage, and finally the results.

Switching back is just removing `DISPLACEMENT_WORKFLOW_MODE` (the default `callback` mode and its tests are untouched).

## Limits that come from keeping the workflow unchanged

* **Both product families only.** The workflow has no GUNW/GOFF filter, so any other choice is refused with a clear message.
* **One run at a time.** The workflow's own concurrency group (`sarguardian-aoi-wkt-data-processing`) allows one running and
  one waiting run; a newer waiting run cancels an older waiting one. The website then shows that job as *cancelled*.
* **Results stay downloadable for the workflow's artifact retention (30 days).** The summary and product table stay in the
  database; the ZIP link reports "expired" afterwards.
* The workflow's own Google Drive uploads still happen inside that repository; the website does not use them.
* The API asks GitHub at most once every 8 seconds per job, so the 2-second UI refresh cannot exhaust the rate limit.

## Troubleshooting

| Message | Usual cause |
|---|---|
| `dispatch was rejected (404)` | Wrong repository/workflow name, or the token cannot see that repository |
| `dispatch was rejected (403)` | Token lacks **Actions: Read and write**, or an organisation blocks fine-grained tokens |
| `dispatch was rejected (422) ... ref` | `GITHUB_ACTIONS_REF` does not exist, or the workflow has no `workflow_dispatch` on that ref |
| `did not return the workflow run id` | GitHub ignored `return_run_details` (e.g. an old GitHub Enterprise Server); the job is refused rather than guessed |
| stays at `github_queued` | Another run holds the concurrency group, or no runner is free |
| `RESULT_ARTIFACT_UNAVAILABLE` | The dashboard artifact was not published, or it expired |
| `RESULT_REQUEST_MISMATCH` | The finished run describes a different AOI or dates than the request (never shown as a result) |
