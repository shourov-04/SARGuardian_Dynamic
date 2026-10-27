import type { DisplacementJob } from '../../src/api/catalog';
import { LANDSLIDE_DISPLACEMENT_PATH, LANDSLIDE_RESULT_PATH } from '../../src/routing';
import { LANGTANG_DATASET } from './contract';

type AvailableVisualization = {
  available: true;
  href: string;
  jobId: string;
  returnPath: typeof LANDSLIDE_DISPLACEMENT_PATH;
};

type UnavailableVisualization = { available: false; reason: string };
export type VisualizationAvailability = AvailableVisualization | UnavailableVisualization;

export type JobVisualizationRouteContext = {
  mode: 'job';
  jobId: string;
  returnPath: typeof LANDSLIDE_DISPLACEMENT_PATH;
};

export type StaticLangtangRouteContext = {
  mode: 'static-langtang';
  jobId: string;
  manifestPath: string;
  returnPath: typeof LANDSLIDE_DISPLACEMENT_PATH;
  sourceRunVersion: string;
};

export type LangtangRouteContext = JobVisualizationRouteContext | StaticLangtangRouteContext;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256 = /^[a-f0-9]{64}$/;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function hasDriveResultBundle(summary: Record<string, unknown>): boolean {
  const artifacts = summary.artifacts;
  return Array.isArray(artifacts) && artifacts.some((value) => {
    const artifact = record(value);
    return artifact?.kind === 'aoi_derived_result_bundle'
      && artifact.path === 'aoi-derived-results.zip'
      && typeof artifact.bytes === 'number'
      && Number.isSafeInteger(artifact.bytes)
      && artifact.bytes > 0
      && typeof artifact.sha256 === 'string'
      && SHA256.test(artifact.sha256);
  });
}

function hasExternalResultBundle(summary: Record<string, unknown>): boolean {
  const artifact = record(summary.github_artifact);
  return summary.source === 'github_artifact'
    && typeof artifact?.name === 'string'
    && /^[A-Za-z0-9._-]{1,200}$/.test(artifact.name);
}

export function visualizationForJob(job: DisplacementJob): VisualizationAvailability {
  const unavailable = (reason: string): UnavailableVisualization => ({ available: false, reason });
  if (job.state !== 'succeeded') return unavailable('Visualization is available only after successful processing.');
  if (!UUID.test(job.id)) return unavailable('Visualization cannot be linked because the processing job identity is invalid.');
  const summary = record(job.result_summary);
  const validation = record(summary?.validation);
  const provenance = record(summary?.provenance);
  if (!summary || summary.scientific_status !== 'PASS' || validation?.status !== 'PASS' || !provenance) {
    return unavailable('Visualization is unavailable because this result has no verified scientific provenance.');
  }
  if (typeof provenance.aoi_sha256 !== 'string' || !SHA256.test(provenance.aoi_sha256)) {
    return unavailable('Visualization is unavailable because the result AOI provenance is invalid.');
  }
  if (job.github_run_id && provenance.github_run_id !== job.github_run_id) {
    return unavailable('Visualization is unavailable because the result run provenance does not match this task.');
  }
  if (!hasDriveResultBundle(summary) && !hasExternalResultBundle(summary)) {
    return unavailable('Visualization is unavailable because this task has no retained derived-result package.');
  }
  const query = new URLSearchParams({
    mode: 'job',
    job: job.id,
    return: LANDSLIDE_DISPLACEMENT_PATH,
  });
  return {
    available: true,
    href: `${LANDSLIDE_RESULT_PATH}?${query.toString()}`,
    jobId: job.id,
    returnPath: LANDSLIDE_DISPLACEMENT_PATH,
  };
}

export function resolveLangtangRouteContext(search: string): LangtangRouteContext | null {
  const query = new URLSearchParams(search);
  const jobId = query.get('job') || '';
  if (query.get('return') !== LANDSLIDE_DISPLACEMENT_PATH || !UUID.test(jobId)) return null;
  if (query.get('mode') === 'job') {
    return { mode: 'job', jobId, returnPath: LANDSLIDE_DISPLACEMENT_PATH };
  }
  if (query.get('dataset') !== LANGTANG_DATASET.datasetId
      || query.get('run') !== LANGTANG_DATASET.sourceRunVersion) return null;
  return {
    mode: 'static-langtang',
    jobId,
    manifestPath: LANGTANG_DATASET.manifestPath,
    returnPath: LANDSLIDE_DISPLACEMENT_PATH,
    sourceRunVersion: LANGTANG_DATASET.sourceRunVersion,
  };
}

export function canOpenLangtangExplorer(featureFlagEnabled: boolean, context: LangtangRouteContext | null): boolean {
  return featureFlagEnabled || context !== null;
}
