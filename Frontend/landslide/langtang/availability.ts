import type { DisplacementJob } from '../../src/api/catalog';
import { LANDSLIDE_DISPLACEMENT_PATH, LANDSLIDE_LANGTANG_PATH } from '../../src/routing';
import { LANGTANG_DATASET } from './contract';

type AvailableVisualization = {
  available: true;
  href: string;
  manifestPath: string;
  returnPath: typeof LANDSLIDE_DISPLACEMENT_PATH;
  sourceRunVersion: string;
};

type UnavailableVisualization = { available: false; reason: string };
export type VisualizationAvailability = AvailableVisualization | UnavailableVisualization;

export type LangtangRouteContext = {
  jobId: string;
  manifestPath: string;
  returnPath: typeof LANDSLIDE_DISPLACEMENT_PATH;
  sourceRunVersion: string;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function matchesLangtangAoi(wkt: string): boolean {
  const polygon = /^\s*POLYGON\s*\(\(([^()]+)\)\)\s*$/i.exec(wkt);
  if (!polygon) return false;
  const coordinates = polygon[1].split(',').map((pair) => pair.trim().split(/\s+/).map(Number));
  return coordinates.length === LANGTANG_DATASET.aoiCoordinates.length
    && coordinates.every((pair, index) => pair.length === 2
      && pair.every(Number.isFinite)
      && Math.abs(pair[0] - LANGTANG_DATASET.aoiCoordinates[index][0]) < 1e-9
      && Math.abs(pair[1] - LANGTANG_DATASET.aoiCoordinates[index][1]) < 1e-9);
}

export function visualizationForJob(job: DisplacementJob): VisualizationAvailability {
  const unavailable = (reason: string): UnavailableVisualization => ({ available: false, reason });
  if (job.state !== 'succeeded') return unavailable('Visualization is available only after successful processing.');
  const summary = record(job.result_summary);
  const validation = record(summary?.validation);
  const provenance = record(summary?.provenance);
  const artifact = record(summary?.github_artifact);
  if (!summary || summary.scientific_status !== 'PASS' || validation?.status !== 'PASS' || !provenance) {
    return unavailable('Visualization is unavailable because this result has no verified scientific provenance.');
  }
  const runIds = [job.github_run_id, provenance.github_run_id, artifact?.run_id]
    .filter((value): value is string => typeof value === 'string' && value.length > 0);
  if (!runIds.length || runIds.some((runId) => runId !== LANGTANG_DATASET.sourceRunId)) {
    return unavailable('Visualization is not available for this processing run.');
  }
  if (artifact?.run_attempt !== undefined && artifact.run_attempt !== LANGTANG_DATASET.sourceRunAttempt) {
    return unavailable('Visualization is not available for this workflow attempt.');
  }
  if (provenance.aoi_sha256 !== LANGTANG_DATASET.aoiSha256
      || !matchesLangtangAoi(job.aoi_wkt)
      || job.start_date !== LANGTANG_DATASET.startDate
      || job.end_date !== LANGTANG_DATASET.endDate) {
    return unavailable('Visualization is not available for this AOI and date window.');
  }
  if (!UUID.test(job.id)) return unavailable('Visualization cannot be linked because the processing job identity is invalid.');
  const query = new URLSearchParams({
    dataset: LANGTANG_DATASET.datasetId,
    run: LANGTANG_DATASET.sourceRunVersion,
    job: job.id,
    return: LANDSLIDE_DISPLACEMENT_PATH,
  });
  return {
    available: true,
    href: `${LANDSLIDE_LANGTANG_PATH}?${query.toString()}`,
    manifestPath: LANGTANG_DATASET.manifestPath,
    returnPath: LANDSLIDE_DISPLACEMENT_PATH,
    sourceRunVersion: LANGTANG_DATASET.sourceRunVersion,
  };
}

export function resolveLangtangRouteContext(search: string): LangtangRouteContext | null {
  const query = new URLSearchParams(search);
  const jobId = query.get('job') || '';
  if (query.get('dataset') !== LANGTANG_DATASET.datasetId
      || query.get('run') !== LANGTANG_DATASET.sourceRunVersion
      || query.get('return') !== LANDSLIDE_DISPLACEMENT_PATH
      || !UUID.test(jobId)) return null;
  return {
    jobId,
    manifestPath: LANGTANG_DATASET.manifestPath,
    returnPath: LANDSLIDE_DISPLACEMENT_PATH,
    sourceRunVersion: LANGTANG_DATASET.sourceRunVersion,
  };
}

export function canOpenLangtangExplorer(featureFlagEnabled: boolean, context: LangtangRouteContext | null): boolean {
  return featureFlagEnabled || context !== null;
}
