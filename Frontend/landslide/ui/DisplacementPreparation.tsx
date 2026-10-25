import { useCallback, useEffect, useState } from 'react';
import type { User } from 'firebase/auth';
import type { PreparedInvestigation } from '../engine/investigation';
import {
  cancelDisplacementJob,
  getDisplacementJob,
  listDisplacementJobs,
  resubmitDisplacementJob,
  DEFAULT_DISPLACEMENT_OPTIONS,
  type DisplacementProcessingOptions,
  type DisplacementJob,
} from '../../src/api/catalog';
import { ScientificResults } from './ScientificResults';
import { safeHttpsUrl } from '../engine/scientificResults';
import { ELAPSED_TICK_MS, formatElapsed, type GranuleCounts } from './processingStatus';

const activeStates = new Set(['queued', 'dispatching', 'running']);
const noopTaskActivityChange = () => {};
export const JOB_STATUS_POLL_MS = 2000;

function resultGranuleCounts(job: DisplacementJob): GranuleCounts | null {
  const products = job.result_summary?.products;
  if (Array.isArray(products)) return {
      GUNW: products.filter((product) => product && typeof product === 'object' && (product as Record<string, unknown>).product_type === 'GUNW').length,
      GOFF: products.filter((product) => product && typeof product === 'object' && (product as Record<string, unknown>).product_type === 'GOFF').length,
      source: 'processed-results',
    };
  const discovered = job.result_summary?.catalog_granule_counts;
  if (discovered && typeof discovered === 'object') {
    const counts = discovered as Record<string, unknown>;
    if (Number.isInteger(counts.GUNW) && Number.isInteger(counts.GOFF)) {
      return { GUNW: counts.GUNW as number, GOFF: counts.GOFF as number, source: 'worker-discovery' };
    }
  }
  return null;
}

export function DisplacementJobStatus({ job, busy, clock, granuleCounts, onCancel, onRefresh, onRetry, onBack, user = null }: {
  job: DisplacementJob;
  user?: Pick<User, 'getIdToken'> | null;
  busy: boolean;
  clock: number;
  granuleCounts: GranuleCounts | null;
  onCancel: () => void;
  onRefresh: () => void;
  onRetry: () => void;
  onBack: () => void;
}) {
  const hasActiveJob = activeStates.has(job.state);
  const counts = resultGranuleCounts(job) || granuleCounts;
  const runUrl = safeHttpsUrl(job.github_run_url);
  return <section className="displacement-job-status" aria-live="polite">
    <h2>Processing status</h2>
    <p><b>Status:</b> {job.state}</p><p><b>Job ID:</b> {job.id}</p>
    <p><b>Stage:</b> {job.stage}</p>
    {runUrl && <p><b>GitHub run:</b> <a href={runUrl} target="_blank" rel="noreferrer">open</a></p>}
    <p><b>Submitted:</b> {new Date(job.created_at).toLocaleString()}</p>
    <p><b>Age:</b> <time data-testid="job-age">{formatElapsed(job.created_at, job.updated_at, job.state, clock)}</time></p>
    {counts && <div className="catalog-stats displacement-granule-counts" aria-label="Granule counts">
      <div><span>GUNW granules</span><strong>{counts.GUNW}</strong></div>
      <div><span>GOFF granules</span><strong>{counts.GOFF}</strong></div>
      <div><span>Total granules</span><strong>{counts.GUNW + counts.GOFF}</strong></div>
      <div><span>Count source</span><strong>{counts.source === 'processed-results' ? 'Processed results' : counts.source === 'worker-discovery' ? 'Worker discovery' : 'ASF catalogue preflight'}</strong></div>
    </div>}
    {counts?.source === 'catalog-preflight' && <p className="catalog-notice">Catalogue matches for this AOI and date window. A validation pilot may process a smaller eligible subset.</p>}
    {job.error_message && <p role="alert">{job.error_message}</p>}
    {hasActiveJob && <div className="displacement-task-actions">
      <button type="button" className="demo-button danger-button" disabled={busy} onClick={onCancel}>{busy ? 'Cancelling…' : 'Cancel task'}</button>
      <button type="button" className="investigate-button" disabled={busy} onClick={onRefresh}>Refresh task</button>
    </div>}
    {job.state === 'failed' && <div className="displacement-task-actions">
      <button type="button" className="investigate-button" disabled={busy} onClick={onRetry}>{busy ? 'Retrying…' : 'Retry task'}</button>
      <button type="button" className="demo-button" disabled={busy} onClick={onBack}>Go to map</button>
    </div>}
    <ScientificResults job={job} user={user} />
  </section>;
}

function chooseAccountJob(jobs: DisplacementJob[], preparation: PreparedInvestigation, options: DisplacementProcessingOptions) {
  const matching = preparation.valid
    ? jobs.filter((candidate) => candidate.aoi_wkt === preparation.payload.aoi_wkt
      && candidate.start_date === preparation.payload.start_date
      && candidate.end_date === preparation.payload.end_date
      && (candidate.product_type || 'BOTH') === options.product_type
      && (candidate.acquisition_window || 'secondary-acquisition-in-window') === options.acquisition_window
      && (candidate.retention_mode || 'derived-only') === options.retention_mode
      && (candidate.processing_profile || 'full-aoi') === options.processing_profile)
    : [];
  // The API returns newest-first. Once no task is running, reopen the newest
  // matching job (or newest account job) so an older successful result can
  // never masquerade as the current failed request.
  return matching.find((candidate) => activeStates.has(candidate.state))
    || jobs.find((candidate) => activeStates.has(candidate.state))
    || matching[0]
    || jobs[0]
    || null;
}

export function DisplacementPreparation({ preparation, options = DEFAULT_DISPLACEMENT_OPTIONS, granuleCounts = null, onBack, onTaskActivityChange = noopTaskActivityChange, user }: { preparation: PreparedInvestigation; options?: DisplacementProcessingOptions; granuleCounts?: GranuleCounts | null; onBack: () => void; onTaskActivityChange?: (active: boolean) => void; user: User }) {
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<DisplacementJob | null>(null);
  const [jobError, setJobError] = useState('');
  const [accountLoaded, setAccountLoaded] = useState(false);
  const [clock, setClock] = useState(() => Date.now());
  const jobId = job?.id;
  const hasActiveJob = Boolean(job && activeStates.has(job.state));

  const loadAccountJob = useCallback(async () => {
    setJobError('');
    try {
      const jobs = await listDisplacementJobs(user);
      setJob((current) => current && activeStates.has(current.state) ? current : chooseAccountJob(jobs, preparation, options));
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not load your displacement jobs.');
    } finally {
      setAccountLoaded(true);
    }
  }, [options, preparation, user]);

  // Browser refresh and reopening this route only read durable account state.
  useEffect(() => { void loadAccountJob(); }, [loadAccountJob]);

  const refreshCurrentJob = useCallback(async () => {
    if (!jobId) return;
    const nextJob = await getDisplacementJob(jobId, user);
    setJob(nextJob);
    setJobError('');
  }, [jobId, user]);

  useEffect(() => {
    if (!jobId || !hasActiveJob) return undefined;
    let stopped = false;
    let requestInFlight = false;
    const refresh = () => {
      if (stopped || requestInFlight) return;
      requestInFlight = true;
      void getDisplacementJob(jobId, user)
        .then((nextJob) => {
          if (stopped) return;
          setJob(nextJob);
          setJobError('');
        })
        .catch((error: unknown) => {
          if (!stopped) setJobError(error instanceof Error ? error.message : 'Could not refresh the displacement job.');
        })
        .finally(() => { requestInFlight = false; });
    };
    refresh();
    const timer = window.setInterval(() => {
      refresh();
    }, JOB_STATUS_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [hasActiveJob, jobId, user]);

  // The displayed elapsed time is local UI state, not a server poll. Keep it
  // smooth without turning the account-status API into a once-per-second poll.
  useEffect(() => {
    if (!hasActiveJob) return undefined;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), ELAPSED_TICK_MS);
    return () => window.clearInterval(timer);
  }, [hasActiveJob]);

  useEffect(() => {
    if (job) onTaskActivityChange(activeStates.has(job.state));
  }, [job, onTaskActivityChange]);

  // This route is only for an account task already created on the map. It is
  // never a second submission screen, and a direct/expired route returns to
  // the map once the account lookup has completed.
  useEffect(() => {
    if (accountLoaded && !job && !jobError) onBack();
  }, [accountLoaded, job, jobError, onBack]);

  const cancel = useCallback(async () => {
    if (!job || !activeStates.has(job.state) || busy) return;
    setBusy(true);
    setJobError('');
    try {
      const cancelled = await cancelDisplacementJob(job.id, user);
      setJob(cancelled);
      if (cancelled.state === 'cancelled' && cancelled.stage === 'cancelled_by_user') {
        onTaskActivityChange(false);
        onBack();
      }
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not cancel the displacement job.');
    } finally {
      setBusy(false);
    }
  }, [busy, job, onBack, onTaskActivityChange, user]);

  const refreshManually = useCallback(async () => {
    if (!job || busy) return;
    setBusy(true);
    setJobError('');
    try {
      await refreshCurrentJob();
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not refresh the displacement job.');
    } finally {
      setBusy(false);
    }
  }, [busy, job, refreshCurrentJob]);

  const retryFailed = useCallback(async () => {
    if (!job || job.state !== 'failed' || busy) return;
    setBusy(true);
    setJobError('');
    try {
      const replacement = await resubmitDisplacementJob(job.id, crypto.randomUUID(), user);
      setJob(replacement);
      onTaskActivityChange(true);
    } catch (error) {
      setJobError(error instanceof Error ? error.message : 'Could not retry the displacement job.');
    } finally {
      setBusy(false);
    }
  }, [busy, job, onTaskActivityChange, user]);

  if (!job && !jobError) return null;

  return <main className="nisar-results-main displacement-preparation">
    <section className="nisar-results-summary">
      <span className="result-kicker">DISPLACEMENT PROCESSING</span>
      <h1>Find Displacements</h1>
      <p>Your processing task belongs to this signed-in account and continues if you close the browser. Reopen this page to see its current state.</p>
      {job && <DisplacementJobStatus job={job} busy={busy} clock={clock} granuleCounts={granuleCounts} user={user}
        onCancel={() => void cancel()} onRefresh={() => void refreshManually()}
        onRetry={() => void retryFailed()} onBack={onBack} />}
      {jobError && <><p role="alert">{jobError}</p>{!job && <button type="button" className="demo-button" onClick={onBack}>Go to map</button>}</>}
    </section>
  </main>;
}
