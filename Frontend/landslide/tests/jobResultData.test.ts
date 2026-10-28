import JSZip from 'jszip';
import { writeArrayBuffer } from 'geotiff';
import { describe, expect, it } from 'vitest';
import type { DisplacementJob } from '../../src/api/catalog';
import {
  colorForJobValue,
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
  const raster = (bands: number[][]) => writeArrayBuffer(new Float32Array(
    bands[0].flatMap((_value, pixel) => bands.map((band) => band[pixel])),
  ), {
    width: 2,
    height: 2,
    BitsPerSample: bands.map(() => 32),
    SampleFormat: bands.map(() => 3),
    SamplesPerPixel: bands.length,
    ModelPixelScale: [0.01, 0.01, 0],
    ModelTiepoint: [0, 0, 0, 85, 29, 0],
    GeographicTypeGeoKey: 4326,
    GTModelTypeGeoKey: 2,
    GTRasterTypeGeoKey: 1,
    GDAL_NODATA: 'nan',
  });
  zip.file('product-data/GOFF-001/GOFF_20260101_20260102_PR_HH-layer2.tif', raster([
    [0, Number.NaN, 2, -3], [10, 11, 12, 13], [0.5, Number.NaN, 0.7, 0.8],
  ]));
  zip.file('product-data/GOFF-002/GOFF_20260102_20260103_PR_HH-layer2.tif', raster([
    [0, Number.NaN, 4, -1], [20, 21, 22, 23], [0.9, Number.NaN, 0.5, 0.4],
  ]));
  zip.file('product-data/GUNW-003/GUNW_20260101_20260103_PR_HH.tif', raster([
    [1, 2, 3, 4], [0.1, 0.2, 0.3, 0.4],
  ]));
  return zip.generateAsync({ type: 'blob' });
}

describe('task result raster package', () => {
  it('decodes a verified package and preserves valid zero versus NaN cells', async () => {
    const progress: string[] = [];
    let browserTaskRan = false;
    setTimeout(() => { browserTaskRan = true; }, 0);
    const dataset = await parseJobResultPackage(await resultPackage(), job(), (message) => progress.push(message));
    expect(dataset.products).toHaveLength(2);
    expect(dataset.products.map((product) => product.id)).toEqual(['GOFF-COMBINED', 'GUNW-COMBINED']);
    const product = dataset.products[0];
    expect(product).toMatchObject({ family: 'GOFF', sourceCount: 2, aggregation: 'pixel_median' });
    expect(product.sourceProductIds).toEqual(['GOFF-001', 'GOFF-002']);
    expect(product.bands.map((band) => band.key)).toEqual([
      'slant_range_offset', 'along_track_offset', 'quality',
    ]);

    const validZero = sampleJobMeasurement(product, product.bands[0], 85.005, 28.995);
    expect(validZero).toMatchObject({ valid: true, value: 0, column: 0, row: 0 });
    expect(validZero.quality).toBeCloseTo(0.7, 5);
    const noData = sampleJobMeasurement(product, product.bands[0], 85.015, 28.995);
    expect(noData).toMatchObject({ valid: false, value: null, quality: null, column: 1, row: 0 });
    const medianValue = sampleJobMeasurement(product, product.bands[0], 85.005, 28.985);
    expect(medianValue).toMatchObject({ valid: true, value: 3, column: 0, row: 1 });
    expect(medianValue.quality).toBeCloseTo(0.6, 5);
    expect(progress).toContain('Decoding scientific raster 1 of 3...');
    expect(progress.some((message) => message.startsWith('Combining GOFF rasters'))).toBe(true);
    expect(browserTaskRan).toBe(true);
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

  it('uses high-contrast display colors while preserving zero and no-data distinction', () => {
    const band = {
      sourceBand: 1,
      key: 'displacement' as const,
      label: 'LOS displacement',
      unit: 'mm' as const,
      values: new Float32Array(),
      displayRange: [-100, 100] as [number, number],
    };

    expect(colorForJobValue(Number.NaN, band)).toEqual([0, 0, 0, 0]);
    expect(colorForJobValue(0, band)[3]).toBeGreaterThan(0);
    const subtlePositive = colorForJobValue(5, band);
    expect(subtlePositive[0]).toBeGreaterThan(subtlePositive[1]);
    expect(subtlePositive[0]).toBeGreaterThan(subtlePositive[2]);
    expect(subtlePositive[3]).toBeGreaterThanOrEqual(220);
    const strongPositive = colorForJobValue(100, band);
    expect(strongPositive[0]).toBeGreaterThan(strongPositive[1] * 5);
    expect(strongPositive[0]).toBeGreaterThan(strongPositive[2] * 2);
  });
});
