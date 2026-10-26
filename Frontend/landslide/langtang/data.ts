import proj4 from 'proj4';
import type { LangtangBand, LangtangBandKey, LangtangManifest, LangtangProduct, PixelMeasurement } from './types';

export const LANGTANG_MANIFEST_PATH = '/data/nisar/langtang/40212069747-1/manifest.json';
export const LANGTANG_SCHEMA = 'sarguardian-langtang-web-data/1.0';

const EXPECTED_PRODUCT_CONTRACT = {
  'GOFF-005': {
    referenceDate: '2026-07-23',
    secondaryDate: '2026-08-16',
    sourceSha256: '3ed36031d8d5b05e8c14a3c870c579801991e277bff0637164650f8ac062680e',
  },
  'GOFF-009': {
    referenceDate: '2026-08-16',
    secondaryDate: '2026-08-28',
    sourceSha256: '20aa7c9d412cd16a83e7ab34c9d460a901372d0502ea24f3808b2c188ff7d311',
  },
} as const;
const EXPECTED_PRODUCTS = new Set(Object.keys(EXPECTED_PRODUCT_CONTRACT));
const EXPECTED_BANDS = new Set<LangtangBandKey>(['slant_range_offset', 'along_track_offset', 'correlation']);
const SHA256 = /^[a-f0-9]{64}$/;
const EXPECTED_TRANSFORM = [80, 0, 340720, 0, -80, 3138000];
const EXPECTED_BOUNDS = [340720, 3118240, 369840, 3138000];
const EXPECTED_AOI_WKT = 'POLYGON ((85.463902 28.357602, 85.378759 28.208853, 85.610158 28.184949, 85.670584 28.305019, 85.463902 28.357602))';

const browserFetch: typeof fetch = (input, init) => globalThis.fetch(input, init);

function isNumberTuple(value: unknown, length: number): value is number[] {
  return Array.isArray(value) && value.length === length && value.every((item) => Number.isFinite(item));
}

function isRelativeAssetPath(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && !value.startsWith('/')
    && !value.includes('..')
    && !value.includes('\\')
    && !value.includes('://');
}

export function parseLangtangManifest(value: unknown): LangtangManifest {
  if (!value || typeof value !== 'object') throw new Error('Langtang data manifest is not an object.');
  const manifest = value as Partial<LangtangManifest>;
  if (manifest.schema_version !== LANGTANG_SCHEMA || manifest.dataset_id !== 'langtang-40212069747-1') {
    throw new Error('Langtang data manifest schema is unsupported.');
  }
  if (
    manifest.source_run_id !== '40212069747'
    || manifest.source_run_attempt !== 1
    || manifest.processing_status !== 'PASS'
    || manifest.science_status !== 'PASS_WITH_LIMITATIONS'
    || manifest.maturity !== 'PROVISIONAL'
    || manifest.pixel_stack_status !== 'COMPLETED_NO_DETECTION'
    || manifest.candidate_screening_status !== 'SKIPPED_UNSUPPORTED_CUSTOM_AOI'
    || manifest.historical_observation !== true
    || manifest.hazard_warning !== false
  ) {
    throw new Error('Langtang provenance contract is incomplete.');
  }
  if (
    !manifest.aoi
    || manifest.aoi.crs !== 'EPSG:4326'
    || manifest.aoi.wkt !== EXPECTED_AOI_WKT
    || !Array.isArray(manifest.aoi.coordinates)
  ) {
    throw new Error('Langtang AOI is missing or invalid.');
  }
  if (!Array.isArray(manifest.products) || manifest.products.length !== 2) {
    throw new Error('Langtang manifest must contain the two verified GOFF products.');
  }
  const commonOffsetRange = manifest.offset_display_range_mm;
  if (
    !isNumberTuple(commonOffsetRange, 2)
    || commonOffsetRange[0] >= 0
    || commonOffsetRange[1] <= 0
    || commonOffsetRange[0] !== -commonOffsetRange[1]
  ) {
    throw new Error('Langtang offset display range must be finite, symmetric, and centered at zero.');
  }
  const ids = new Set<string>();
  for (const product of manifest.products) {
    if (!EXPECTED_PRODUCTS.has(product.id) || ids.has(product.id)) throw new Error('Langtang product identity is invalid.');
    ids.add(product.id);
    const expected = EXPECTED_PRODUCT_CONTRACT[product.id as keyof typeof EXPECTED_PRODUCT_CONTRACT];
    if (
      product.family !== 'GOFF'
      || product.maturity !== 'PROVISIONAL'
      || product.track !== 'D048'
      || product.reference_date !== expected.referenceDate
      || product.secondary_date !== expected.secondaryDate
      || product.source_sha256 !== expected.sourceSha256
      || product.crs !== 'EPSG:32645'
      || product.width !== 364
      || product.height !== 247
      || product.nodata !== 'NaN'
      || !isNumberTuple(product.transform, 6)
      || product.transform.some((value, index) => value !== EXPECTED_TRANSFORM[index])
      || !isNumberTuple(product.bounds_native, 4)
      || product.bounds_native.some((value, index) => value !== EXPECTED_BOUNDS[index])
      || !Array.isArray(product.footprint_wgs84)
    ) throw new Error(`Langtang product ${product.id} has invalid geospatial metadata.`);
    if (!Array.isArray(product.bands) || product.bands.length !== 3) throw new Error(`Langtang product ${product.id} is missing bands.`);
    const bandKeys = new Set<string>();
    for (const band of product.bands) {
      if (!EXPECTED_BANDS.has(band.key) || bandKeys.has(band.key)) throw new Error(`Langtang product ${product.id} has invalid bands.`);
      bandKeys.add(band.key);
      const expectedBand = {
        slant_range_offset: { sourceBand: 1, unit: 'mm' },
        along_track_offset: { sourceBand: 2, unit: 'mm' },
        correlation: { sourceBand: 3, unit: 'unitless' },
      }[band.key];
      if (
        band.source_band !== expectedBand.sourceBand
        || band.unit !== expectedBand.unit
        || (band.numeric_dtype !== 'float32-le' && band.numeric_dtype !== 'float64-le')
        || !isRelativeAssetPath(band.numeric?.url)
        || !SHA256.test(band.numeric.sha256)
        || band.numeric.bytes !== product.width * product.height * (band.numeric_dtype === 'float32-le' ? 4 : 8)
        || !isRelativeAssetPath(band.preview?.url)
        || !SHA256.test(band.preview.sha256)
        || !isNumberTuple(band.preview.bounds_wgs84, 4)
        || !isNumberTuple(band.display_range, 2)
        || band.display_only !== true
      ) throw new Error(`Langtang band ${band.key} has an invalid asset contract.`);
      const expectedRange = band.key === 'correlation' ? [0, 1] : commonOffsetRange;
      if (band.display_range.some((value, index) => value !== expectedRange[index])) {
        throw new Error(`Langtang band ${band.key} does not use the verified common display scale.`);
      }
    }
  }
  return manifest as LangtangManifest;
}

export async function loadLangtangManifest(
  manifestPath = LANGTANG_MANIFEST_PATH,
  fetcher: typeof fetch = browserFetch,
): Promise<LangtangManifest> {
  const response = await fetcher(manifestPath, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`Verified Langtang raster assets are unavailable (${response.status}).`);
  return parseLangtangManifest(await response.json());
}

export function findBand(product: LangtangProduct, key: LangtangBandKey): LangtangBand {
  const band = product.bands.find((candidate) => candidate.key === key);
  if (!band) throw new Error(`Band ${key} is absent from ${product.id}.`);
  return band;
}

export function resolveAssetUrl(manifestPath: string, assetPath: string): string {
  const base = new URL(manifestPath, window.location.origin);
  return new URL(assetPath, base).toString();
}

export class NumericRasterCache {
  private readonly buffers = new Map<string, Promise<ArrayBuffer>>();

  constructor(private readonly fetcher: typeof fetch = browserFetch) {}

  load(url: string, expectedBytes: number, expectedSha256: string): Promise<ArrayBuffer> {
    const cacheKey = `${url}#${expectedSha256}`;
    const existing = this.buffers.get(cacheKey);
    if (existing) return existing;
    const pending = this.fetcher(url).then(async (response) => {
      if (!response.ok) throw new Error(`Numeric raster asset is unavailable (${response.status}).`);
      const buffer = await response.arrayBuffer();
      if (buffer.byteLength !== expectedBytes) throw new Error('Numeric raster asset size verification failed.');
      const digest = await globalThis.crypto.subtle.digest('SHA-256', buffer);
      const actualSha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
      if (actualSha256 !== expectedSha256) throw new Error('Numeric raster asset checksum verification failed.');
      return buffer;
    });
    this.buffers.set(cacheKey, pending);
    return pending;
  }
}

function numericValue(buffer: ArrayBuffer, index: number, dtype: LangtangBand['numeric_dtype']): number {
  const bytesPerValue = dtype === 'float32-le' ? 4 : 8;
  const offset = index * bytesPerValue;
  if (offset < 0 || offset + bytesPerValue > buffer.byteLength) throw new Error('Numeric raster asset size does not match its grid.');
  const view = new DataView(buffer);
  return dtype === 'float32-le' ? view.getFloat32(offset, true) : view.getFloat64(offset, true);
}

function nativePixel(product: LangtangProduct, longitude: number, latitude: number): [column: number, row: number] | null {
  const [x, y] = proj4('EPSG:4326', product.proj4, [longitude, latitude]);
  const [a, b, c, d, e, f] = product.transform;
  const determinant = a * e - b * d;
  if (!Number.isFinite(determinant) || determinant === 0) throw new Error('Raster affine transform is not invertible.');
  const cornerColumn = (e * (x - c) - b * (y - f)) / determinant;
  const cornerRow = (-d * (x - c) + a * (y - f)) / determinant;
  const column = Math.round(cornerColumn - 0.5);
  const row = Math.round(cornerRow - 0.5);
  return column >= 0 && row >= 0 && column < product.width && row < product.height ? [column, row] : null;
}

export async function sampleMeasurement(
  product: LangtangProduct,
  bandKey: LangtangBandKey,
  longitude: number,
  latitude: number,
  manifestPath: string,
  cache: NumericRasterCache,
): Promise<PixelMeasurement> {
  const band = findBand(product, bandKey);
  const pixel = nativePixel(product, longitude, latitude);
  if (!pixel) return { longitude, latitude, column: null, row: null, valid: false, value: null, unit: band.unit, correlation: null };
  const [column, row] = pixel;
  const index = row * product.width + column;
  const correlationBand = findBand(product, 'correlation');
  const valueUrl = resolveAssetUrl(manifestPath, band.numeric.url);
  const correlationUrl = resolveAssetUrl(manifestPath, correlationBand.numeric.url);
  const [valueBuffer, correlationBuffer] = await Promise.all([
    cache.load(valueUrl, band.numeric.bytes, band.numeric.sha256),
    band.key === 'correlation'
      ? cache.load(valueUrl, band.numeric.bytes, band.numeric.sha256)
      : cache.load(correlationUrl, correlationBand.numeric.bytes, correlationBand.numeric.sha256),
  ]);
  const value = numericValue(valueBuffer, index, band.numeric_dtype);
  const correlation = numericValue(correlationBuffer, index, correlationBand.numeric_dtype);
  const valid = Number.isFinite(value);
  return {
    longitude,
    latitude,
    column,
    row,
    valid,
    value: valid ? value : null,
    unit: band.unit,
    correlation: Number.isFinite(correlation) ? correlation : null,
  };
}
