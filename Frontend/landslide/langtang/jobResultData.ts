import JSZip from 'jszip';
import proj4 from 'proj4';
import { fromArrayBuffer, type TypedArray } from 'geotiff';
import type { User } from 'firebase/auth';
import {
  downloadDisplacementArtifact,
  getDisplacementResultLink,
  type DisplacementJob,
} from '../../src/api/catalog';

export type JobBandKey = 'displacement' | 'slant_range_offset' | 'along_track_offset' | 'quality';

export type JobRasterBand = {
  sourceBand: number;
  key: JobBandKey;
  label: string;
  unit: 'mm' | 'unitless';
  values: TypedArray;
  displayRange: [number, number];
};

export type JobRasterProduct = {
  id: string;
  family: 'GUNW' | 'GOFF';
  sourcePath: string;
  sourceProductIds: string[];
  sourceCount: number;
  aggregation: 'source' | 'pixel_median';
  referenceDate: string;
  secondaryDate: string;
  width: number;
  height: number;
  epsg: number;
  projection: string;
  boundsNative: [number, number, number, number];
  boundsWgs84: [west: number, south: number, east: number, north: number];
  footprintWgs84: Array<[longitude: number, latitude: number]>;
  bands: JobRasterBand[];
};

export type JobRasterDataset = {
  jobId: string;
  runId: string;
  startDate: string;
  endDate: string;
  maturity: string;
  scienceStatus: string;
  aoiCoordinates: Array<[longitude: number, latitude: number]>;
  products: JobRasterProduct[];
};

export type JobPixelMeasurement = {
  longitude: number;
  latitude: number;
  column: number | null;
  row: number | null;
  valid: boolean;
  value: number | null;
  quality: number | null;
  unit: JobRasterBand['unit'];
};

type ResultArtifact = { index: number; bytes: number; sha256: string };
const MAX_PACKAGE_BYTES = 250 * 1024 * 1024;
const MAX_RASTER_BYTES = 32 * 1024 * 1024;
const MAX_PRODUCTS = 100;
const SHA256 = /^[a-f0-9]{64}$/;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function driveBundle(job: DisplacementJob): ResultArtifact | null {
  const artifacts = job.result_summary?.artifacts;
  if (!Array.isArray(artifacts)) return null;
  const index = artifacts.findIndex((value) => {
    const artifact = record(value);
    return artifact?.kind === 'aoi_derived_result_bundle' && artifact.path === 'aoi-derived-results.zip';
  });
  if (index < 0) return null;
  const artifact = record(artifacts[index]);
  if (!artifact || typeof artifact.bytes !== 'number' || !Number.isSafeInteger(artifact.bytes)
      || artifact.bytes <= 0 || artifact.bytes > MAX_PACKAGE_BYTES
      || typeof artifact.sha256 !== 'string' || !SHA256.test(artifact.sha256)) return null;
  return { index, bytes: artifact.bytes, sha256: artifact.sha256 };
}

async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function loadJobResultBlob(job: DisplacementJob, user: Pick<User, 'getIdToken'>): Promise<Blob> {
  const retained = driveBundle(job);
  if (retained) {
    const blob = await downloadDisplacementArtifact(job.id, retained.index, user);
    if (blob.size !== retained.bytes || await sha256(blob) !== retained.sha256) {
      throw new Error('The downloaded result package no longer matches its validated inventory.');
    }
    return blob;
  }
  if (job.result_summary?.source === 'github_artifact') {
    const link = await getDisplacementResultLink(job.id, user);
    const response = await fetch(link.url, { cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error('The retained result package could not be downloaded.');
    const blob = await response.blob();
    if (!blob.size || blob.size > MAX_PACKAGE_BYTES) throw new Error('The retained result package has an invalid size.');
    return blob;
  }
  throw new Error('This completed task has no retained derived-result package.');
}

export function parseAoiPolygon(wkt: string): Array<[number, number]> {
  const polygon = /^\s*POLYGON\s*\(\(([^()]+)\)\)\s*$/i.exec(wkt);
  if (!polygon) throw new Error('The processing AOI is not a supported polygon.');
  const coordinates = polygon[1].split(',').map((pair) => {
    const values = pair.trim().split(/\s+/).map(Number);
    if (values.length !== 2 || values.some((value) => !Number.isFinite(value))) {
      throw new Error('The processing AOI contains invalid coordinates.');
    }
    return values as [number, number];
  });
  if (coordinates.length < 4 || coordinates.length > 10_000) throw new Error('The processing AOI has an invalid vertex count.');
  return coordinates;
}

function projectionForEpsg(epsg: number): string {
  if (epsg === 4326) return 'EPSG:4326';
  if (epsg >= 32601 && epsg <= 32660) return `+proj=utm +zone=${epsg - 32600} +datum=WGS84 +units=m +no_defs`;
  if (epsg >= 32701 && epsg <= 32760) return `+proj=utm +zone=${epsg - 32700} +south +datum=WGS84 +units=m +no_defs`;
  throw new Error(`The result raster projection EPSG:${epsg} is not supported by the map viewer.`);
}

function finitePercentile(values: TypedArray, percentile: number): number {
  const finite: number[] = [];
  for (let index = 0; index < values.length; index += 1) {
    const value = Number(values[index]);
    if (Number.isFinite(value)) finite.push(Math.abs(value));
  }
  if (!finite.length) return 1;
  finite.sort((a, b) => a - b);
  return Math.max(finite[Math.min(finite.length - 1, Math.floor((finite.length - 1) * percentile))], 1e-6);
}

function datesFromName(name: string): [string, string] {
  const match = /_(\d{8})_(\d{8})(?:_|\.)/.exec(name);
  const format = (value: string) => `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}`;
  return match ? [format(match[1]), format(match[2])] : ['Unknown', 'Unknown'];
}

function bandDefinitions(family: 'GUNW' | 'GOFF', rasters: TypedArray[]): JobRasterBand[] {
  const expected = family === 'GUNW'
    ? [
      { key: 'displacement' as const, label: 'LOS displacement', unit: 'mm' as const },
      { key: 'quality' as const, label: 'Coherence', unit: 'unitless' as const },
    ]
    : [
      { key: 'slant_range_offset' as const, label: 'Slant-range offset', unit: 'mm' as const },
      { key: 'along_track_offset' as const, label: 'Along-track offset', unit: 'mm' as const },
      { key: 'quality' as const, label: 'Correlation', unit: 'unitless' as const },
    ];
  if (rasters.length !== expected.length) throw new Error(`${family} result raster has an unexpected band count.`);
  return expected.map((definition, index) => {
    const clip = definition.unit === 'unitless' ? 1 : finitePercentile(rasters[index], 0.98);
    return {
      sourceBand: index + 1,
      ...definition,
      values: rasters[index],
      displayRange: definition.unit === 'unitless' ? [0, 1] : [-clip, clip],
    };
  });
}

function wgs84Footprint(projection: string, bounds: [number, number, number, number]): Array<[number, number]> {
  const [minX, minY, maxX, maxY] = bounds;
  const corners: Array<[number, number]> = [[minX, maxY], [maxX, maxY], [maxX, minY], [minX, minY], [minX, maxY]];
  return corners.map(([x, y]) => proj4(projection, 'EPSG:4326', [x, y]) as [number, number]);
}

function median(values: number[]): number {
  if (!values.length) return Number.NaN;
  values.sort((left, right) => left - right);
  const middle = Math.floor(values.length / 2);
  return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

function combinedDate(products: JobRasterProduct[], key: 'referenceDate' | 'secondaryDate', latest: boolean): string {
  const dates = products.map((product) => product[key]).filter((value) => /^\d{4}-\d{2}-\d{2}$/.test(value));
  if (!dates.length) return 'Unknown';
  dates.sort();
  return dates[latest ? dates.length - 1 : 0];
}

function sampleNativeBand(product: JobRasterProduct, bandKey: JobBandKey, x: number, y: number, projection: string): number {
  const [sourceX, sourceY] = projection === product.projection
    ? [x, y]
    : proj4(projection, product.projection, [x, y]);
  const [minX, minY, maxX, maxY] = product.boundsNative;
  const column = Math.floor((sourceX - minX) / (maxX - minX) * product.width);
  const row = Math.floor((maxY - sourceY) / (maxY - minY) * product.height);
  if (column < 0 || row < 0 || column >= product.width || row >= product.height) return Number.NaN;
  const band = product.bands.find((candidate) => candidate.key === bandKey);
  return band ? Number(band.values[row * product.width + column]) : Number.NaN;
}

export function combineJobProducts(products: JobRasterProduct[]): JobRasterProduct[] {
  const combined: JobRasterProduct[] = [];
  for (const family of ['GOFF', 'GUNW'] as const) {
    const sources = products.filter((product) => product.family === family);
    if (!sources.length) continue;
    const reference = sources.reduce((largest, product) => (
      product.width * product.height > largest.width * largest.height ? product : largest
    ));
    const projection = reference.projection;
    const projectedCorners = sources.flatMap((product) => product.footprintWgs84.map(([longitude, latitude]) => (
      proj4('EPSG:4326', projection, [longitude, latitude]) as [number, number]
    )));
    const xValues = projectedCorners.map(([x]) => x);
    const yValues = projectedCorners.map(([, y]) => y);
    const boundsNative: [number, number, number, number] = [
      Math.min(...xValues), Math.min(...yValues), Math.max(...xValues), Math.max(...yValues),
    ];
    const referenceResolutionX = (reference.boundsNative[2] - reference.boundsNative[0]) / reference.width;
    const referenceResolutionY = (reference.boundsNative[3] - reference.boundsNative[1]) / reference.height;
    const width = Math.max(1, Math.ceil((boundsNative[2] - boundsNative[0]) / referenceResolutionX));
    const height = Math.max(1, Math.ceil((boundsNative[3] - boundsNative[1]) / referenceResolutionY));
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width * height > 10_000_000) {
      throw new Error(`${family} combined visualization grid is too large.`);
    }
    const expectedBands = reference.bands.map(({ key, label, unit }) => ({ key, label, unit }));
    if (sources.some((product) => expectedBands.some((band) => !product.bands.some((candidate) => (
      candidate.key === band.key && candidate.unit === band.unit
    ))))) {
      throw new Error(`${family} result rasters do not have a consistent band inventory.`);
    }
    const combinedValues = expectedBands.map(() => new Float32Array(width * height).fill(Number.NaN));
    const [minX, minY, maxX, maxY] = boundsNative;
    for (let row = 0; row < height; row += 1) {
      const y = maxY - (row + 0.5) / height * (maxY - minY);
      for (let column = 0; column < width; column += 1) {
        const x = minX + (column + 0.5) / width * (maxX - minX);
        const index = row * width + column;
        expectedBands.forEach((band, bandIndex) => {
          const finite = sources
            .map((product) => sampleNativeBand(product, band.key, x, y, projection))
            .filter(Number.isFinite);
          combinedValues[bandIndex][index] = median(finite);
        });
      }
    }
    const footprintWgs84 = wgs84Footprint(projection, boundsNative);
    const longitudes = footprintWgs84.map(([longitude]) => longitude);
    const latitudes = footprintWgs84.map(([, latitude]) => latitude);
    const sourceProductIds = [...new Set(sources.flatMap((product) => product.sourceProductIds))]
      .sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
    combined.push({
      id: `${family}-COMBINED`,
      family,
      sourcePath: `combined:${sourceProductIds.join(',')}`,
      sourceProductIds,
      sourceCount: sourceProductIds.length,
      aggregation: 'pixel_median',
      referenceDate: combinedDate(sources, 'referenceDate', false),
      secondaryDate: combinedDate(sources, 'secondaryDate', true),
      width,
      height,
      epsg: reference.epsg,
      projection,
      boundsNative,
      boundsWgs84: [Math.min(...longitudes), Math.min(...latitudes), Math.max(...longitudes), Math.max(...latitudes)],
      footprintWgs84,
      bands: bandDefinitions(family, combinedValues),
    });
  }
  return combined;
}

function validateSummary(summary: Record<string, unknown>, job: DisplacementJob): void {
  const provenance = record(job.result_summary?.provenance);
  if (summary.status !== 'PASS' || summary.date_start !== job.start_date || summary.date_end !== job.end_date
      || typeof summary.aoi_wkt_sha256 !== 'string' || summary.aoi_wkt_sha256 !== provenance?.aoi_sha256
      || typeof summary.run_id !== 'string' || (job.github_run_id && !summary.run_id.startsWith(`${job.github_run_id}-`))) {
    throw new Error('The retained result package does not match this processing task.');
  }
}

export async function parseJobResultPackage(blob: Blob, job: DisplacementJob): Promise<JobRasterDataset> {
  if (!blob.size || blob.size > MAX_PACKAGE_BYTES) throw new Error('The retained result package has an invalid size.');
  const zip = await JSZip.loadAsync(blob, { checkCRC32: true, createFolders: false });
  const entries = Object.values(zip.files).filter((entry) => !entry.dir);
  if (entries.some((entry) => entry.name.startsWith('/') || entry.name.split('/').includes('..'))) {
    throw new Error('The retained result package contains an unsafe path.');
  }
  const summaries = entries.filter((entry) => entry.name === 'aoi-run-summary.json' || entry.name.endsWith('/aoi-run-summary.json'));
  if (summaries.length !== 1) throw new Error('The retained result package does not contain one run summary.');
  const summaryText = await summaries[0].async('string');
  if (summaryText.length > 2_000_000) throw new Error('The retained run summary is too large.');
  const parsed: unknown = JSON.parse(summaryText);
  const summary = record(parsed);
  if (!summary) throw new Error('The retained run summary is invalid.');
  validateSummary(summary, job);

  const tiffs = entries.filter((entry) => /(^|\/)product-data\/[^/]+\/[^/]+\.tiff?$/i.test(entry.name));
  if (!tiffs.length || tiffs.length > MAX_PRODUCTS) throw new Error('The retained result package has an invalid raster inventory.');
  const products: JobRasterProduct[] = [];
  for (const entry of tiffs) {
    const family = /(^|\/)GUNW-/i.test(entry.name) ? 'GUNW' : /(^|\/)GOFF-/i.test(entry.name) ? 'GOFF' : null;
    if (!family) throw new Error('The retained package contains an unsupported result raster.');
    const productMatch = /(^|\/)(GUNW|GOFF)-(\d+)\//i.exec(entry.name);
    if (!productMatch) throw new Error('A retained result raster has no product identity.');
    const bytes = await entry.async('arraybuffer');
    if (!bytes.byteLength || bytes.byteLength > MAX_RASTER_BYTES) throw new Error('A retained result raster has an invalid size.');
    const tiff = await fromArrayBuffer(bytes);
    const image = await tiff.getImage();
    const geoKeys = image.getGeoKeys();
    const epsg = Number(geoKeys?.ProjectedCSTypeGeoKey || geoKeys?.GeographicTypeGeoKey);
    if (!Number.isInteger(epsg)) throw new Error('A retained result raster has no supported EPSG code.');
    const projection = projectionForEpsg(epsg);
    const width = image.getWidth();
    const height = image.getHeight();
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width <= 0 || height <= 0 || width * height > 10_000_000) {
      throw new Error('A retained result raster has invalid dimensions.');
    }
    const rasterResult = await image.readRasters({ interleave: false });
    const rasters = Array.from(rasterResult) as TypedArray[];
    const rawBounds = image.getBoundingBox();
    if (rawBounds.length !== 4 || rawBounds.some((value) => !Number.isFinite(value))) throw new Error('A retained result raster has invalid bounds.');
    const boundsNative = rawBounds as [number, number, number, number];
    const footprintWgs84 = wgs84Footprint(projection, boundsNative);
    const longitudes = footprintWgs84.map(([longitude]) => longitude);
    const latitudes = footprintWgs84.map(([, latitude]) => latitude);
    const [referenceDate, secondaryDate] = datesFromName(entry.name);
    products.push({
      id: `${family}-${productMatch[3].padStart(3, '0')}`,
      family,
      sourcePath: entry.name,
      sourceProductIds: [`${family}-${productMatch[3].padStart(3, '0')}`],
      sourceCount: 1,
      aggregation: 'source',
      referenceDate,
      secondaryDate,
      width,
      height,
      epsg,
      projection,
      boundsNative,
      boundsWgs84: [Math.min(...longitudes), Math.min(...latitudes), Math.max(...longitudes), Math.max(...latitudes)],
      footprintWgs84,
      bands: bandDefinitions(family, rasters),
    });
  }
  products.sort((left, right) => left.id.localeCompare(right.id, undefined, { numeric: true }));
  const combinedProducts = combineJobProducts(products);
  const diagnostics = record(summary.science_diagnostics);
  return {
    jobId: job.id,
    runId: String(summary.run_id),
    startDate: job.start_date,
    endDate: job.end_date,
    maturity: typeof summary.maturity === 'string' ? summary.maturity : 'PROVISIONAL',
    scienceStatus: typeof diagnostics?.status === 'string' ? diagnostics.status : 'PASS',
    aoiCoordinates: parseAoiPolygon(job.aoi_wkt),
    products: combinedProducts,
  };
}

export function colorForJobValue(value: number, band: JobRasterBand): [number, number, number, number] {
  if (!Number.isFinite(value)) return [0, 0, 0, 0];
  if (band.unit === 'unitless') {
    const t = Math.max(0, Math.min(1, value));
    const strength = Math.pow(t, 0.55);
    return [Math.round(41 + 214 * strength), Math.round(21 + 203 * strength), Math.round(82 - 61 * strength), 238];
  }
  const clip = Math.max(Math.abs(band.displayRange[0]), Math.abs(band.displayRange[1]), 1e-6);
  const t = Math.max(-1, Math.min(1, value / clip));
  if (t === 0) return [205, 214, 219, 175];
  const strength = Math.pow(Math.abs(t), 0.42);
  if (t < 0) {
    return [
      Math.round(201 - 174 * strength),
      Math.round(215 - 107 * strength),
      Math.round(225 - 20 * strength),
      Math.round(210 + 38 * strength),
    ];
  }
  return [
    Math.round(232 - 112 * strength),
    Math.round(202 - 190 * strength),
    Math.round(199 - 157 * strength),
    Math.round(214 + 34 * strength),
  ];
}

function mercatorY(latitude: number): number {
  const limited = Math.max(-85.05112878, Math.min(85.05112878, latitude));
  return Math.log(Math.tan(Math.PI / 4 + limited * Math.PI / 360));
}

function inverseMercatorY(value: number): number {
  return Math.atan(Math.sinh(value)) * 180 / Math.PI;
}

export function renderJobOverlay(product: JobRasterProduct, band: JobRasterBand): string {
  const { canvas, context, pixels } = createJobOverlayCanvas(product);
  paintJobOverlayRows(product, band, pixels, 0, product.height);
  context.putImageData(pixels, 0, 0);
  return canvas.toDataURL('image/png');
}

function createJobOverlayCanvas(product: JobRasterProduct): {
  canvas: HTMLCanvasElement;
  context: CanvasRenderingContext2D;
  pixels: ImageData;
} {
  const canvas = document.createElement('canvas');
  canvas.width = product.width;
  canvas.height = product.height;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('The browser cannot render the result overlay.');
  const pixels = context.createImageData(product.width, product.height);
  return { canvas, context, pixels };
}

function paintJobOverlayRows(
  product: JobRasterProduct,
  band: JobRasterBand,
  pixels: ImageData,
  startRow: number,
  endRow: number,
): void {
  const [west, south, east, north] = product.boundsWgs84;
  const northY = mercatorY(north);
  const southY = mercatorY(south);
  const [minX, minY, maxX, maxY] = product.boundsNative;
  for (let row = startRow; row < endRow; row += 1) {
    const latitude = inverseMercatorY(northY + (southY - northY) * ((row + 0.5) / product.height));
    for (let column = 0; column < product.width; column += 1) {
      const longitude = west + (east - west) * ((column + 0.5) / product.width);
      const [x, y] = proj4('EPSG:4326', product.projection, [longitude, latitude]);
      const sourceColumn = Math.floor((x - minX) / (maxX - minX) * product.width);
      const sourceRow = Math.floor((maxY - y) / (maxY - minY) * product.height);
      const output = (row * product.width + column) * 4;
      const rgba = sourceColumn >= 0 && sourceRow >= 0 && sourceColumn < product.width && sourceRow < product.height
        ? colorForJobValue(Number(band.values[sourceRow * product.width + sourceColumn]), band)
        : [0, 0, 0, 0];
      pixels.data.set(rgba, output);
    }
  }
}

function yieldBrowserTask(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

export async function renderJobOverlayResponsive(product: JobRasterProduct, band: JobRasterBand): Promise<string> {
  const { canvas, context, pixels } = createJobOverlayCanvas(product);
  const rowsPerChunk = Math.max(1, Math.floor(200_000 / product.width));
  for (let startRow = 0; startRow < product.height; startRow += rowsPerChunk) {
    paintJobOverlayRows(product, band, pixels, startRow, Math.min(product.height, startRow + rowsPerChunk));
    if (startRow + rowsPerChunk < product.height) await yieldBrowserTask();
  }
  context.putImageData(pixels, 0, 0);
  return canvas.toDataURL('image/png');
}

export function sampleJobMeasurement(product: JobRasterProduct, band: JobRasterBand, longitude: number, latitude: number): JobPixelMeasurement {
  const [x, y] = proj4('EPSG:4326', product.projection, [longitude, latitude]);
  const [minX, minY, maxX, maxY] = product.boundsNative;
  const column = Math.floor((x - minX) / (maxX - minX) * product.width);
  const row = Math.floor((maxY - y) / (maxY - minY) * product.height);
  if (column < 0 || row < 0 || column >= product.width || row >= product.height) {
    return { longitude, latitude, column: null, row: null, valid: false, value: null, quality: null, unit: band.unit };
  }
  const index = row * product.width + column;
  const rawValue = Number(band.values[index]);
  const qualityBand = product.bands.find((candidate) => candidate.key === 'quality');
  const rawQuality = qualityBand ? Number(qualityBand.values[index]) : Number.NaN;
  return {
    longitude,
    latitude,
    column,
    row,
    valid: Number.isFinite(rawValue),
    value: Number.isFinite(rawValue) ? rawValue : null,
    quality: Number.isFinite(rawQuality) ? rawQuality : null,
    unit: band.unit,
  };
}
