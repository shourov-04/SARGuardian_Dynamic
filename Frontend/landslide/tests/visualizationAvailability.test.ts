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
    aoi_wkt: LANGTANG_DATASET.aoiWkt,
    start_date: '2026-09-01',
    end_date: '2026-10-01',
    github_run_id: '987654321',
    created_at: '2026-10-01T16:00:00Z',
    updated_at: '2026-10-01T17:00:00Z',
    result_summary: {
      scientific_status: 'PASS',
      validation: { status: 'PASS' },
      provenance: { aoi_sha256: 'a'.repeat(64), github_run_id: '987654321' },
      artifacts: [{
        path: 'aoi-derived-results.zip', kind: 'aoi_derived_result_bundle',
        bytes: 12345, sha256: 'b'.repeat(64), drive_file_id: 'private-file-id-123',
      }],
    },
    ...overrides,
  };
}

describe('job-scoped displacement visualization availability', () => {
  it('creates and resolves a route for every verified completed result package', () => {
    const availability = visualizationForJob(eligibleJob());
    expect(availability.available).toBe(true);
    if (!availability.available) throw new Error(availability.reason);
    const url = new URL(availability.href, 'https://example.test');
    expect(url.pathname).toBe('/landslide/result');
    expect(url.searchParams.get('mode')).toBe('job');
    expect(url.searchParams.get('job')).toBe(JOB_ID);
    expect(url.searchParams.get('return')).toBe(LANDSLIDE_DISPLACEMENT_PATH);
    expect(resolveLangtangRouteContext(url.search)).toEqual({
      mode: 'job', jobId: JOB_ID, returnPath: LANDSLIDE_DISPLACEMENT_PATH,
    });
  });

  it('supports a verified external workflow result package', () => {
    const job = eligibleJob({
      result_summary: {
        source: 'github_artifact', scientific_status: 'PASS', validation: { status: 'PASS' },
        provenance: { aoi_sha256: 'a'.repeat(64), github_run_id: '987654321' },
        github_artifact: { name: 'sarguardian-aoi-dashboard-987654321-1', run_id: '987654321', run_attempt: 1 },
      },
    });
    expect(visualizationForJob(job).available).toBe(true);
  });

  it.each(['queued', 'dispatching', 'running', 'failed'] as const)('does not expose the viewer for %s jobs', (state) => {
    expect(visualizationForJob(eligibleJob({ state })).available).toBe(false);
  });

  it('fails closed for invalid status, provenance, run identity or missing retained package', () => {
    const cases: DisplacementJob[] = [
      eligibleJob({ id: 'not-a-job-id' }),
      eligibleJob({ result_summary: { scientific_status: 'FAIL', validation: { status: 'PASS' }, provenance: { aoi_sha256: 'a'.repeat(64), github_run_id: '987654321' } } }),
      eligibleJob({ result_summary: { scientific_status: 'PASS', validation: { status: 'PASS' }, provenance: { aoi_sha256: 'bad', github_run_id: '987654321' } } }),
      eligibleJob({ result_summary: { scientific_status: 'PASS', validation: { status: 'PASS' }, provenance: { aoi_sha256: 'a'.repeat(64), github_run_id: 'wrong' }, artifacts: [{ path: 'aoi-derived-results.zip', kind: 'aoi_derived_result_bundle', bytes: 1, sha256: 'b'.repeat(64) }] } }),
      eligibleJob({ result_summary: { scientific_status: 'PASS', validation: { status: 'PASS' }, provenance: { aoi_sha256: 'a'.repeat(64), github_run_id: '987654321' }, artifacts: [] } }),
    ];
    for (const job of cases) expect(visualizationForJob(job).available).toBe(false);
  });

  it('preserves the exact legacy Langtang context for the optional static demo', () => {
    const query = new URLSearchParams({
      dataset: LANGTANG_DATASET.datasetId,
      run: LANGTANG_DATASET.sourceRunVersion,
      job: JOB_ID,
      return: LANDSLIDE_DISPLACEMENT_PATH,
    });
    const context = resolveLangtangRouteContext(`?${query.toString()}`);
    expect(context).toEqual({
      mode: 'static-langtang',
      jobId: JOB_ID,
      manifestPath: LANGTANG_DATASET.manifestPath,
      returnPath: LANDSLIDE_DISPLACEMENT_PATH,
      sourceRunVersion: LANGTANG_DATASET.sourceRunVersion,
    });
    expect(canOpenLangtangExplorer(false, context)).toBe(true);
    expect(canOpenLangtangExplorer(false, null)).toBe(false);
  });

  it('rejects forged job routes', () => {
    expect(resolveLangtangRouteContext('?mode=job&job=../../x&return=/landslide/displacements')).toBeNull();
    expect(resolveLangtangRouteContext(`?mode=job&job=${JOB_ID}&return=https://evil.example`)).toBeNull();
  });
});
