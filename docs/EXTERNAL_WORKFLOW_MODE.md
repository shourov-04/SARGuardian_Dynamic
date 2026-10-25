# Optional external GitHub workflow mode

`DISPLACEMENT_WORKFLOW_MODE=external` lets the account API dispatch the existing
`sarguardian-aoi-wkt-dashboard.yml` workflow in another repository. The default remains
`callback`, which uses this repository's production callback workflow and Drive storage.

```text
website -> account API --workflow_dispatch--> external repository workflow
   ^           | polls the exact run ID                 |
   |           | reads the validated report             v
   +-----------+<--------------------------- dashboard artifact
```

The API enforces the same fixed production contract used by callback mode: both product
families, secondary-acquisition date filtering, derived-only retention and full-AOI
processing. Those values and the submitted AOI and dates become the external workflow's
six declared inputs.

The API polls GitHub at most once every eight seconds per job. It reads only the small
report and manifest members with HTTP range requests and verifies that the successful
report belongs to the submitted AOI, dates and exact run. The browser receives no GitHub
token. On success, the authenticated job owner can request a short-lived signed URL and
the page shows one **Download results** button.

## One-time setup

1. In the account that owns the workflow repository, create a fine-grained personal
   access token limited to that repository with **Actions: Read and write**.
2. Configure the API environment and redeploy:

   | Variable | Value |
   |---|---|
   | `DISPLACEMENT_WORKFLOW_MODE` | `external` |
   | `DISPLACEMENT_ACTIONS_ENABLED` | `true` |
   | `GITHUB_ACTIONS_REPOSITORY` | `moshiour0/SARGuardian-backend` |
   | `GITHUB_ACTIONS_WORKFLOW` | `sarguardian-aoi-wkt-dashboard.yml` |
   | `GITHUB_ACTIONS_REF` | `main` |
   | `GITHUB_ACTIONS_TOKEN` | repository-scoped token from step 1 |

   `DISPLACEMENT_CALLBACK_SECRET` and `PUBLIC_API_URL` are not required in external mode.
   No database migration is required.
3. Submit an AOI from the website. The task page shows the stage, discovered counts and,
   after success, the single result download button.

Do not change mode while an external job is active. After it reaches a terminal state,
removing `DISPLACEMENT_WORKFLOW_MODE` restores the default callback path.

## Operational limits

* Product family and processing options remain server-controlled and are not shown as
  user choices.
* The external workflow's concurrency group can leave one run waiting while another runs.
* GitHub result ZIPs remain available only for that repository's artifact retention period.
* The external repository's own Drive uploads still occur there; this website downloads
  its published GitHub result artifact.

## Troubleshooting

| Message | Usual cause |
|---|---|
| `dispatch was rejected (404)` | Wrong repository/workflow name, or the token cannot see it |
| `dispatch was rejected (403)` | Token lacks Actions read/write, or organization policy blocks it |
| `dispatch was rejected (422)` | The configured ref or workflow inputs do not match |
| `did not return the workflow run id` | The GitHub endpoint did not return trackable run details |
| `RESULT_ARTIFACT_UNAVAILABLE` | The dashboard artifact was not published or has expired |
| `RESULT_REQUEST_MISMATCH` | The report describes a different AOI or date window |
