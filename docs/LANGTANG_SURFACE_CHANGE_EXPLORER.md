# Langtang Surface Change Explorer (SG-001)

SG-001 is an additive, feature-flagged historical research view at
`/landslide/langtang`. It does not change the displacement-processing pipeline,
authentication, APIs, Drive retention, or the existing map and results routes.

## Verified input contract

The offline exporter accepts only the completed dashboard bundle from GitHub
Actions run `40212069747`, attempt 1. The extracted bundle must contain:

- `aoi-run-summary.json`
- `product-data/GOFF-005/GOFF_20260723_20260816_PR_HH-layer2.tif`
- `product-data/GOFF-009/GOFF_20260816_20260828_PR_HH-layer2.tif`

Before writing output, the exporter verifies the run summary, both source
SHA-256 values, grid shape, three-band layout, `EPSG:32645` CRS, affine transform,
bounds, floating-point dtype, and NaN nodata. It hashes the inputs again after
export and fails if either TIFF changed.

```powershell
python tools/visualization/export_langtang.py `
  --input-root C:\path\to\sarguardian-aoi-dashboard-40212069747-1 `
  --output-root Frontend\public\data\nisar\langtang\40212069747-1
```

The output directory must not already exist. The exporter creates:

- a versioned, validated `manifest.json`;
- transparent display-only Web Mercator PNG overlays;
- native row-major little-endian numeric band files for pixel inspection; and
- SHA-256 and byte-size records for every exported asset.

Both offset bands use one symmetric blue-white-red display range centered at
zero across both products. Correlation uses a fixed zero-to-one scalar range.
Visual reprojection uses nearest-neighbour resampling so no values are
interpolated through NaN cells. Map clicks are transformed from WGS84 into the
native UTM grid and read from the numeric band file; PNG colors are never used
as measurements.

## Local preview

Keep the feature disabled until the verified assets above have been exported.
Then set the public browser flag in `Frontend/.env.local`:

```dotenv
VITE_LANGTANG_EXPLORER_ENABLED=true
```

From `Frontend/`, run `npm ci` and `npm run dev`, sign in, and open
`http://localhost:5173/landslide/langtang`. The route uses the existing Google
Maps browser key and Firebase authentication. With the flag off, existing users
see no new investigation card and direct navigation shows a closed state.

The two inputs are provisional D048 GOFF acquisition pairs. The 16–28 August
pair spans the reported 26 August event; it does not locate a change at one
instant. Slant-range and along-track offsets are radar-coordinate observations,
not calibrated horizontal or 3D motion. NaN means no measurement, while a
finite zero is a valid measurement. Correlation is a quality indicator, not a
probability. The view is historical research output and is not a hazard warning.

## Data publication state

The two TIFFs and run summary were locally verified from the supplied
`aoi-derived-results.zip`. The exporter reproduced all six native numeric bands
without changing their values, and ten real source pixels (including NaN cells)
were checked against Rasterio. Generated web assets remain local verification
output and are intentionally not committed or published in SG-001. No
placeholder raster, fabricated measurement, or hard-coded Drive link is used.
Until the verified static output is placed at
`Frontend/public/data/nisar/langtang/40212069747-1/` for a local preview or
published through an approved deployment, the route fails closed with a clear
asset-unavailable message.
