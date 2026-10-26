import { describe, expect, it } from 'vitest';
import type { DisplacementJob } from '../../src/api/catalog';
import { LANDSLIDE_DISPLACEMENT_PATH } from '../../src/routing';
import { canOpenLangtangExplorer, resolveLangtangRouteContext, visualizationForJob } from '../langtang/availability';
import { LANGTANG_DATASET } from '../langtang/contract';

const JOB_ID = '7f3501a3-9d5c-4ff1-8f1c-6b99f90130f1';

function eligibleJob(overrides: Partial<DisplacementJob> = {}): DisplacementJob {
  return {
    id: JOB_ID,
    state: 'succeeded',
    stage: 'completed',
    aoi_wkt: 'POLYGON ((85.463902000000004 28.357602, 85.378759000000002 28.208853000000001, 85.610157999999998 28.184949, 85.670584000000005 28.305019000000001, 85.463902000000004 28.357602))',
    start_date: LANGTANG_DATASET.startDate,
    end_date: LANGTANG_DATASET.endDate,
    github_run_id: LANGTANG_DATASET.sourceRunId,
    created_at: '2026-10-25T16:00:00Z',
    updated_at: '2026-10-25T17:00:00Z',
    result_summary: {
      scientific_status: 'PASS',
      validation: { status: 'PASS' },
      provenance: {
        aoi_sha256: LANGTANG_DATASET.aoiSha256,
        github_run_id: LANGTANG_DATASET.sourceRunId,
      },
      github_artifact: {
        run_id: LANGTANG_DATASET.sourceRunId,
        run_attempt: LANGTANG_DATASET.sourceRunAttempt,
      },
    },
    ...overrides,
  };
}

describe('run-scoped Langtang visualization availability', () => {
  it('creates and resolves a route only for the verified completed run', () => {
    const availability = visualizationForJob(eligibleJob());
    expect(availability.available).toBe(true);
    if (!availability.available) throw new Error(availability.reason);
    const url = new URL(availability.href, 'https://example.test');
    expect(url.pathname).toBe('/landslide/langtang');
    expect(url.searchParams.get('dataset')).toBe(LANGTANG_DATASET.datasetId);
    expect(url.searchParams.get('run')).toBe(LANGTANG_DATASET.sourceRunVersion);
    expect(url.searchParams.get('job')).toBe(JOB_ID);
    expect(url.searchParams.get('return')).toBe(LANDSLIDE_DISPLACEMENT_PATH);
    expect(resolveLangtangRouteContext(url.search)).toEqual({
      jobId: JOB_ID,
      manifestPath: LANGTANG_DATASET.manifestPath,
      returnPath: LANDSLIDE_DISPLACEMENT_PATH,
      sourceRunVersion: LANGTANG_DATASET.sourceRunVersion,
    });
  });

  it.each(['queued', 'dispatching', 'running', 'failed'] as const)('does not expose the viewer for %s jobs', (state) => {
    expect(visualizationForJob(eligibleJob({ state })).available).toBe(false);
  });

  it('fails closed when run, attempt, AOI, date, provenance or job identity differs', () => {
    const cases: DisplacementJob[] = [
      eligibleJob({ github_run_id: '40212069748' }),
      eligibleJob({ result_summary: {
        scientific_status: 'PASS', validation: { status: 'PASS' },
        provenance: { aoi_sha256: LANGTANG_DATASET.aoiSha256, github_run_id: LANGTANG_DATASET.sourceRunId },
        github_artifact: { run_id: LANGTANG_DATASET.sourceRunId, run_attempt: 2 },
      } }),
      eligibleJob({ aoi_wkt: 'POLYGON ((85 28, 86 28, 86 29, 85 28))' }),
      eligibleJob({ start_date: '2026-07-21' }),
      eligibleJob({ result_summary: { scientific_status: 'PASS', validation: { status: 'PASS' }, provenance: { aoi_sha256: '0'.repeat(64), github_run_id: LANGTANG_DATASET.sourceRunId } } }),
      eligibleJob({ result_summary: { scientific_status: 'NOT_MEASURABLE', validation: { status: 'PASS' }, provenance: { aoi_sha256: LANGTANG_DATASET.aoiSha256, github_run_id: LANGTANG_DATASET.sourceRunId } } }),
      eligibleJob({ id: 'not-a-job-id' }),
    ];
    for (const job of cases) expect(visualizationForJob(job).available).toBe(false);
  });

  it('allows the verified result context with the general feature flag off and rejects forged queries', () => {
    const availability = visualizationForJob(eligibleJob());
    if (!availability.available) throw new Error(availability.reason);
    const search = new URL(availability.href, 'https://example.test').search;
    const context = resolveLangtangRouteContext(search);
    expect(canOpenLangtangExplorer(false, context)).toBe(true);
    expect(canOpenLangtangExplorer(true, null)).toBe(true);
    expect(canOpenLangtangExplorer(false, null)).toBe(false);
    const forged = new URLSearchParams(search);
    forged.set('run', 'wrong');
    expect(resolveLangtangRouteContext(`?${forged.toString()}`)).toBeNull();
    expect(resolveLangtangRouteContext('?dataset=langtang-40212069747-1&run=40212069747-1&job=../../x&return=/landslide/displacements')).toBeNull();
  });
});
