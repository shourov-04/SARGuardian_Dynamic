import JSZip from 'jszip';
import { writeArrayBuffer } from 'geotiff';
import { describe, expect, it } from 'vitest';
import type { DisplacementJob } from '../../src/api/catalog';
import {
  parseAoiPolygon,
  parseJobResultPackage,
  sampleJobMeasurement,
} from '../langtang/jobResultData';

const JOB_ID = '7f3501a3-9d5c-4ff1-8f1c-6b99f90130f1';
const AOI_HASH = 'a'.repeat(64);

function job(): DisplacementJob {
  return {
    id: JOB_ID,
    state: 'succeeded',
    stage: 'completed',
    aoi_wkt: 'POLYGON ((85 28.98, 85.02 28.98, 85.02 29, 85 29, 85 28.98))',
    start_date: '2026-01-01',
    end_date: '2026-01-02',
    github_run_id: '987654321',
    created_at: '2026-01-02T00:00:00Z',
    updated_at: '2026-01-02T00:10:00Z',
    result_summary: { provenance: { aoi_sha256: AOI_HASH } },
  };
}

async function resultPackage(): Promise<Blob> {
  const zip = new JSZip();
  zip.file('aoi-run-summary.json', JSON.stringify({
    status: 'PASS',
    run_id: '987654321-1',
    date_start: '2026-01-01',
    date_end: '2026-01-02',
    aoi_wkt_sha256: AOI_HASH,
    science_diagnostics: { status: 'PASS_WITH_LIMITATIONS' },
  }));
  const tiff = writeArrayBuffer(new Float32Array([
    0, 10, 0.5,
    Number.NaN, 11, Number.NaN,
    2, 12, 0.7,
    -3, 13, 0.8,
  ]), {
    width: 2,
    height: 2,
    BitsPerSample: [32, 32, 32],
    SampleFormat: [3, 3, 3],
    SamplesPerPixel: 3,
    ModelPixelScale: [0.01, 0.01, 0],
    ModelTiepoint: [0, 0, 0, 85, 29, 0],
    GeographicTypeGeoKey: 4326,
    GTModelTypeGeoKey: 2,
    GTRasterTypeGeoKey: 1,
    GDAL_NODATA: 'nan',
  });
  zip.file('product-data/GOFF-001/GOFF_20260101_20260102_PR_HH-layer2.tif', tiff);
  return zip.generateAsync({ type: 'blob' });
}

describe('task result raster package', () => {
  it('decodes a verified package and preserves valid zero versus NaN cells', async () => {
    const dataset = await parseJobResultPackage(await resultPackage(), job());
    expect(dataset.products).toHaveLength(1);
    const product = dataset.products[0];
    expect(product.id).toBe('GOFF-001');
    expect(product.bands.map((band) => band.key)).toEqual([
      'slant_range_offset', 'along_track_offset', 'quality',
    ]);

    const validZero = sampleJobMeasurement(product, product.bands[0], 85.005, 28.995);
    expect(validZero).toMatchObject({ valid: true, value: 0, quality: 0.5, column: 0, row: 0 });
    const noData = sampleJobMeasurement(product, product.bands[0], 85.015, 28.995);
    expect(noData).toMatchObject({ valid: false, value: null, quality: null, column: 1, row: 0 });
  });

  it('rejects a package whose task provenance does not match', async () => {
    const mismatched = job();
    mismatched.start_date = '2025-12-31';
    await expect(parseJobResultPackage(await resultPackage(), mismatched))
      .rejects.toThrow('does not match this processing task');
  });

  it('parses finite polygon coordinates and rejects unsupported WKT', () => {
    expect(parseAoiPolygon(job().aoi_wkt)).toHaveLength(5);
    expect(() => parseAoiPolygon('POINT (85 29)')).toThrow('not a supported polygon');
  });
});
