import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DisplacementJobStatus, DisplacementPreparation, JOB_STATUS_POLL_MS } from '../ui/DisplacementPreparation';
import { ScientificResults } from '../ui/ScientificResults';
import { countCatalogGranules, ELAPSED_TICK_MS, formatElapsed } from '../ui/processingStatus';
import { prepareInvestigation } from '../engine/investigation';
import { isLandslideDisplacementRoute, LANDSLIDE_DISPLACEMENT_PATH, LANDSLIDE_PATH, LANDSLIDE_RESULTS_PATH, navigatePath } from '../../src/routing';

describe('displacement preparation route', () => {
  const user = { getIdToken: vi.fn() } as never;
  it('keeps the AOI payload out of the processing screen', () => {
    const preparation = prepareInvestigation({ coordinates: [[85, 27], [85.001, 27], [85, 27.001]], areaClosed: true, startDate: '2026-10-01', endDate: '2026-10-22', today: '2026-10-23' });
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={preparation} onBack={() => {}} user={user} />);
    expect(html).not.toContain('POLYGON');
    expect(html).not.toContain('Copy WKT');
    expect(html).not.toContain('Copy Payload');
    expect(html).not.toContain('Start displacement processing');
    expect(html).not.toContain('Back to map');
  });
  it('does not render a second submission screen on an empty/direct visit', () => {
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={{ valid: false, errors: ['Select an AOI.'] }} onBack={() => {}} user={user} />);
    expect(html).toBe('');
    expect(html).not.toContain('POLYGON');
    expect(html).not.toContain('Copy Payload');
  });
  it('uses existing navigation for both workflows and back, suppressing duplicate pushes', () => {
    const push = vi.fn();
    let path = navigatePath(LANDSLIDE_RESULTS_PATH, LANDSLIDE_PATH, push);
    expect(path).toBe('/landslide/results');
    path = navigatePath(LANDSLIDE_PATH, path, push);
    path = navigatePath(LANDSLIDE_DISPLACEMENT_PATH, path, push);
    expect(isLandslideDisplacementRoute(path)).toBe(true);
    navigatePath(path, path, push);
    expect(push).toHaveBeenCalledTimes(3);
    expect(navigatePath(LANDSLIDE_PATH, path, push)).toBe('/landslide');
  });
  it('does not expose a second submission control on the task status route', () => {
    const preparation = prepareInvestigation({ coordinates: [[85, 27], [85.001, 27], [85, 27.001]], areaClosed: true, startDate: '2026-10-01', endDate: '2026-10-22', today: '2026-10-23' });
    const html = renderToStaticMarkup(<DisplacementPreparation preparation={preparation} onBack={() => {}} user={user} />);
    expect(html).not.toContain('Start displacement processing');
    expect(html).not.toContain('Preparing displacement job');
  });
  it('keeps a one-second age tick and renders failed recovery actions with granule counts', () => {
    expect(ELAPSED_TICK_MS).toBe(1000);
    expect(JOB_STATUS_POLL_MS).toBe(2000);
    expect(formatElapsed('2026-10-24T10:00:00Z', '2026-10-24T10:00:02Z', 'running', Date.parse('2026-10-24T10:00:03Z'))).toBe('0m 3s');
    expect(countCatalogGranules([
      { product_type: 'GUNW' }, { product_type: 'GOFF' }, { product_type: 'GUNW' },
    ] as never)).toEqual({ GUNW: 2, GOFF: 1, source: 'catalog-preflight' });
    const failedJob = {
      id: 'failed-job', state: 'failed', stage: 'failed', aoi_wkt: 'POLYGON((85 27, 85.01 27, 85 27.01, 85 27))',
      start_date: '2026-09-27', end_date: '2026-10-24', created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:01:05Z',
      error_message: 'Worker failed.',
    } as const;
    const html = renderToStaticMarkup(<DisplacementJobStatus job={failedJob} busy={false} clock={Date.parse('2026-10-24T10:02:00Z')}
      granuleCounts={{ GUNW: 4, GOFF: 3, source: 'catalog-preflight' }} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(html).toContain('Age:');
    expect(html).toContain('1m 5s');
    expect(html).toContain('GUNW granules');
    expect(html).toContain('>4<');
    expect(html).toContain('GOFF granules');
    expect(html).toContain('>3<');
    expect(html).toContain('Retry task');
    expect(html).toContain('Go to map');
    expect(html).not.toContain('Start displacement processing');

    const runningWithWorkerCounts = { ...failedJob, state: 'running', stage: 'products_discovered', result_summary: {
      catalog_granule_counts: { GUNW: 8, GOFF: 5 }, selected_granule_counts: { GUNW: 1, GOFF: 1 },
    } } as const;
    const runningHtml = renderToStaticMarkup(<DisplacementJobStatus job={runningWithWorkerCounts as never} busy={false}
      clock={Date.parse('2026-10-24T10:02:00Z')} granuleCounts={null} onCancel={() => {}} onRefresh={() => {}}
      onRetry={() => {}} onBack={() => {}} />);
    expect(runningHtml).toContain('>8<');
    expect(runningHtml).toContain('>5<');
    expect(runningHtml).toContain('Worker discovery');
  });

  it('shows only one Drive-folder download action after successful processing', () => {
    const succeededJob = {
      id: 'succeeded-job', state: 'succeeded', stage: 'completed',
      aoi_wkt: 'POLYGON((85 27, 85.01 27, 85 27.01, 85 27))',
      start_date: '2026-09-27', end_date: '2026-10-24',
      created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:10:00Z',
      result_summary: {
        storage_verification: { status: 'PASS', remote_folder_id: 'private-drive-folder-123' },
        diagnostics: { pixel_stack: { status: 'PASS' } },
        products: [{ product_type: 'GUNW' }],
        artifacts: [{ path: 'aoi-derived-results.zip' }],
      },
    } as const;

    const html = renderToStaticMarkup(<ScientificResults job={succeededJob as never} />);

    expect(html).toContain('Download results');
    expect(html).toContain('https://drive.google.com/drive/folders/private-drive-folder-123');
    expect(html).not.toContain('Scientific diagnostics');
    expect(html).not.toContain('Input products');
    expect(html).not.toContain('Retained scientific outputs');
  });

  it('offers the GitHub result package for output produced by the unchanged upstream workflow', () => {
    const githubJob = {
      id: 'github-job', state: 'succeeded', stage: 'completed',
      aoi_wkt: 'POLYGON((85 27, 85.01 27, 85 27.01, 85 27))', start_date: '2026-09-27', end_date: '2026-10-24',
      created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:10:00Z',
      github_run_url: 'https://github.com/owner/repo/actions/runs/42',
      result_summary: {
        source: 'github_artifact', scientific_status: 'PASS',
        github_artifact: { name: 'sarguardian-aoi-dashboard-42-1', size_in_bytes: 5_242_880, expires_at: '2026-11-30T00:00:00Z' },
        products: [
          { id: 'GUNW-001', product_type: 'GUNW', status: 'PASS', reference_date: '2026-09-01', secondary_date: '2026-09-13', valid_pixels: 321 },
          { id: 'GOFF-002', product_type: 'GOFF', status: 'WARNING', reference_date: '2026-09-01', secondary_date: '2026-09-13' },
        ],
      },
    } as const;

    const html = renderToStaticMarkup(<ScientificResults job={githubJob as never} user={user} />);

    expect(html).toContain('Download results (ZIP)');
    expect(html).toContain('sarguardian-aoi-dashboard-42-1.zip');
    expect(html).toContain('5.0 MB');
    expect(html).toContain('Processing checks passed');
    expect(html).toContain('2 products processed');
    expect(html).toContain('GUNW-001');
    expect(html).toContain('GOFF-002');
    expect(html).not.toContain('drive.google.com');

    const status = renderToStaticMarkup(<DisplacementJobStatus job={githubJob as never} busy={false} clock={Date.parse('2026-10-24T10:12:00Z')}
      granuleCounts={null} user={user} onCancel={() => {}} onRefresh={() => {}} onRetry={() => {}} onBack={() => {}} />);
    expect(status).toContain('https://github.com/owner/repo/actions/runs/42');
    expect(status).toContain('GUNW granules');
    expect(status).toContain('Download results (ZIP)');
  });

  it('shows nothing for a succeeded job that has neither a Drive folder nor a GitHub package', () => {
    const bare = { id: 'bare', state: 'succeeded', stage: 'completed', aoi_wkt: 'x', start_date: 'a', end_date: 'b',
      created_at: '2026-10-24T10:00:00Z', updated_at: '2026-10-24T10:10:00Z', result_summary: { products: [] } } as const;
    expect(renderToStaticMarkup(<ScientificResults job={bare as never} />)).toBe('');
  });
});
