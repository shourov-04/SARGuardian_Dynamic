import proj4 from 'proj4';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NumericRasterCache, parseLangtangManifest, sampleMeasurement } from '../langtang/data';
import type { LangtangManifest, LangtangProduct } from '../langtang/types';

const transform: LangtangProduct['transform'] = [80, 0, 340720, 0, -80, 3138000];
const projection = '+proj=utm +zone=45 +datum=WGS84 +units=m +no_defs +type=crs';

function product(id: 'GOFF-005' | 'GOFF-009', prefix: string): LangtangProduct {
  return {
    id, family: 'GOFF', maturity: 'PROVISIONAL', track: 'D048',
    reference_date: id === 'GOFF-005' ? '2026-07-23' : '2026-08-16',
    secondary_date: id === 'GOFF-005' ? '2026-08-16' : '2026-08-28',
    pair_semantics: id === 'GOFF-005' ? 'pre-event acquisition pair' : 'event-spanning acquisition pair',
    source_tiff: `${id}.tif`,
    source_sha256: id === 'GOFF-005'
      ? '3ed36031d8d5b05e8c14a3c870c579801991e277bff0637164650f8ac062680e'
      : '20aa7c9d412cd16a83e7ab34c9d460a901372d0502ea24f3808b2c188ff7d311',
    width: 364, height: 247,
    crs: 'EPSG:32645', proj4: projection, transform, bounds_native: [340720, 3118240, 369840, 3138000],
    nodata: 'NaN', pixel_sampling: 'nearest native pixel center',
    footprint_wgs84: [[85.4, 28.2], [85.6, 28.2], [85.6, 28.4], [85.4, 28.2]],
    bands: [
      ['slant_range_offset', 'Slant-range offset', 'mm'],
      ['along_track_offset', 'Along-track offset', 'mm'],
      ['correlation', 'Correlation', 'unitless'],
    ].map(([key, label, unit], index) => ({
      source_band: (index + 1) as 1 | 2 | 3,
      key: key as 'slant_range_offset' | 'along_track_offset' | 'correlation',
      label,
      unit: unit as 'mm' | 'unitless',
      numeric_dtype: 'float32-le',
      numeric: { url: `${prefix}-${key}.f32`, sha256: 'b'.repeat(64), bytes: 364 * 247 * 4 },
      preview: { url: `${prefix}-${key}.png`, sha256: 'c'.repeat(64), bytes: 10, width: 10, height: 10, bounds_wgs84: [85.3, 28.1, 85.7, 28.5], resampling: 'nearest', transparent_nodata: true },
      display_range: key === 'correlation' ? [0, 1] : [-20, 20],
      display_palette: key === 'correlation' ? 'purple-yellow' : 'blue-white-red',
      display_only: true,
    })),
  };
}

function manifest(): LangtangManifest {
  return {
    schema_version: 'sarguardian-langtang-web-data/1.0', dataset_id: 'langtang-40212069747-1',
    source_run_id: '40212069747', source_run_attempt: 1, processing_status: 'PASS',
    science_status: 'PASS_WITH_LIMITATIONS', maturity: 'PROVISIONAL',
    pixel_stack_status: 'COMPLETED_NO_DETECTION', candidate_screening_status: 'SKIPPED_UNSUPPORTED_CUSTOM_AOI',
    historical_observation: true, hazard_warning: false,
    aoi: {
      crs: 'EPSG:4326',
      wkt: 'POLYGON ((85.463902 28.357602, 85.378759 28.208853, 85.610158 28.184949, 85.670584 28.305019, 85.463902 28.357602))',
      coordinates: [[85.463902, 28.357602], [85.378759, 28.208853], [85.610158, 28.184949], [85.670584, 28.305019], [85.463902, 28.357602]],
    },
    offset_display_range_mm: [-20, 20], offset_display_note: 'Display only.',
    products: [product('GOFF-005', '005'), product('GOFF-009', '009')],
  };
}

function pointAt(column: number, row: number): [number, number] {
  const x = transform[0] * (column + 0.5) + transform[2];
  const y = transform[4] * (row + 0.5) + transform[5];
  return proj4(projection, 'EPSG:4326', [x, y]) as [number, number];
}

afterEach(() => vi.unstubAllGlobals());

describe('Langtang verified static-data contract', () => {
  it('accepts only the fixed two-product provenance contract', () => {
    expect(parseLangtangManifest(manifest()).products.map((item) => item.id)).toEqual(['GOFF-005', 'GOFF-009']);
    const invalid = manifest();
    invalid.science_status = 'PASS' as never;
    expect(() => parseLangtangManifest(invalid)).toThrow(/provenance/i);

    const substitutedSource = manifest();
    substitutedSource.products[0].source_sha256 = 'a'.repeat(64);
    expect(() => parseLangtangManifest(substitutedSource)).toThrow(/geospatial metadata/i);

    const externalAsset = manifest();
    externalAsset.products[0].bands[0].numeric.url = 'https://untrusted.example/raster.f32';
    expect(() => parseLangtangManifest(externalAsset)).toThrow(/asset contract/i);
  });

  it('preserves the browser global receiver when using the default numeric fetcher', async () => {
    const bytes = new Uint8Array([0, 0, 0, 0]);
    const sha256 = Array.from(
      new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes)),
      (byte) => byte.toString(16).padStart(2, '0'),
    ).join('');
    const receiverCheckingFetch = vi.fn(function (this: unknown) {
      if (this !== globalThis) throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
      return Promise.resolve(new Response(bytes.slice(), { status: 200 }));
    }) as typeof fetch;
    vi.stubGlobal('fetch', receiverCheckingFetch);

    const cache = new NumericRasterCache();

    await expect(cache.load('https://app.test/value.f32.bin', bytes.byteLength, sha256)).resolves.toBeInstanceOf(ArrayBuffer);
    expect(receiverCheckingFetch).toHaveBeenCalledOnce();
  });

  it('samples 10 native numeric cells across both products and distinguishes zero from NaN', async () => {
    vi.stubGlobal('window', { location: { origin: 'https://app.test' } });
    const gridLength = 364 * 247;
    const buffers = new Map<string, ArrayBuffer>();
    const samples = [
      [4, 5, 0], [12, 18, 1.25], [31, 42, -2.5], [100, 120, Number.NaN], [360, 240, 8.75],
    ] as const;
    for (const prefix of ['005', '009']) {
      const offsets = new Float32Array(gridLength).fill(Number.NaN);
      const correlation = new Float32Array(gridLength).fill(Number.NaN);
      samples.forEach(([column, row, value], index) => {
        offsets[row * 364 + column] = prefix === '005' ? value : (Number.isNaN(value) ? value : value + 10);
        correlation[row * 364 + column] = Number.isNaN(value) ? Number.NaN : 0.7 + index * 0.01;
      });
      buffers.set(`https://app.test/${prefix}-slant_range_offset.f32`, offsets.buffer as ArrayBuffer);
      buffers.set(`https://app.test/${prefix}-correlation.f32`, correlation.buffer as ArrayBuffer);
    }
    const sha256 = async (buffer: ArrayBuffer) => Array.from(
      new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', buffer)),
      (byte) => byte.toString(16).padStart(2, '0'),
    ).join('');
    const fetcher = vi.fn(async (url: string | URL | Request) => {
      const buffer = buffers.get(String(url));
      return buffer ? new Response(buffer.slice(0), { status: 200 }) : new Response('', { status: 404 });
    }) as typeof fetch;
    const cache = new NumericRasterCache(fetcher);
    const current = manifest();
    for (const entry of current.products) {
      for (const band of entry.bands) {
        const buffer = buffers.get(`https://app.test/${band.numeric.url}`);
        if (buffer) band.numeric.sha256 = await sha256(buffer);
      }
    }
    const outcomes = await Promise.all(current.products.flatMap((entry) => samples.map(async ([column, row]) => {
      const [longitude, latitude] = pointAt(column, row);
      return sampleMeasurement(entry, 'slant_range_offset', longitude, latitude, '/manifest.json', cache);
    })));

    expect(outcomes).toHaveLength(10);
    expect(outcomes[0]).toMatchObject({ valid: true, value: 0, column: 4, row: 5 });
    expect(outcomes[3]).toMatchObject({ valid: false, value: null, correlation: null });
    expect(outcomes[5]).toMatchObject({ valid: true, value: 10, column: 4, row: 5 });
    expect(outcomes.filter((outcome) => outcome.valid)).toHaveLength(8);
    expect(outcomes[1].correlation).toBeCloseTo(0.71, 5);
  });
});
