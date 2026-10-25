# moshiour0 streaming-batch integration

This repository vendors the dynamic-AOI form of the processing architecture from
`moshiour0/SARGuardian-backend` at commit
`2218dfeb73353080d8d94bd733d9e7bd9a848886` (2026-10-23).

The fixed-source workflow `sarguardian-streaming-batch.yml` cannot accept a user
polygon. The same source repository provides its custom-WKT counterpart in
`sarguardian-aoi-wkt-dashboard.yml` and `sarguardian-aoi-product-worker.yml`.
Those workflows and their processing tools are the integration source used here.

## Processing sequence retained from upstream

1. Validate and canonicalize the submitted single-ring EPSG:4326 WKT polygon.
2. Search ASF separately by GUNW/GOFF and BETA/PROVISIONAL maturity, using the
   exact polygon and the selected acquisition-date policy.
3. Deduplicate release variants and create a deterministic product manifest.
4. Process products sequentially through the pinned SARGuardian science commit
   `10639a916c9829850775410b3ed7de62e527770d`.
5. Download one exact granule into an isolated directory, verify HDF5 identity,
   and confirm its product grid intersects the submitted AOI.
6. Inventory source datasets and attributes; optionally retain a full-lossless
   HDF5 copy.
7. Run the original `gunw_reader.py` or `goff_reader.py` with AOI `custom`.
8. Validate AOI-specific CSV/GeoTIFF outputs and provenance before retention.
9. Upload required outputs to private Drive and verify remote size/checksum.
10. Delete runner-local raw/archive files only after verified remote retention.
11. Retry only a product without a complete processing/cleanup receipt pair.
12. Aggregate verified products into the HTML dashboard, CSV, JSON reports,
    quicklooks, and downloadable derived-results ZIP.

## Local integration adaptations

- The account-facing `sarguardian-displacement.yml` remains the dispatch entry
  point expected by the existing API and frontend.
- Signed callbacks bind the upstream workflow run to the durable account job,
  persist discovered GUNW/GOFF counts, and accept success only when the upstream
  dashboard and private-Drive receipt form a complete version-2 result contract.
- Existing `product_type` (`BOTH`, `GUNW`, `GOFF`) is applied during catalog
  discovery. Account jobs always use upstream `full-aoi-baseline`, so every
  eligible product in the requested AOI/date baseline is selected. The pilot
  profile remains available only when the reusable upstream workflow is run
  directly for engineering validation.
- ASF catalog calls are bounded to two 30-second attempts. Username/password
  authentication exchanges credentials for an EDL bearer token with a
  20-second timeout and skips the unrelated `asf-urs` cookie flow, which can
  reject valid NISAR L2 access. Exact file probes use two bounded attempts and
  the upstream worker still performs one download per product attempt.
- A download explicitly classified as non-retryable is terminal and is not
  scheduled for the product-level retry pass.
- Either `GOOGLE_DRIVE_PARENT_FOLDER_ID` (upstream name) or the existing
  `GOOGLE_DRIVE_FOLDER_ID` secret can provide the private retention root.

Frontend source is intentionally outside this integration and remains unchanged.
