import { useState } from 'react';
import type { User } from 'firebase/auth';
import { getDisplacementResultLink, type DisplacementJob } from '../../src/api/catalog';
import { readGithubResultPackage } from '../engine/scientificResults';

type ResultUser = Pick<User, 'getIdToken'> | null;

function driveFolderUrl(job: DisplacementJob): string | undefined {
  if (job.state !== 'succeeded' || !job.result_summary) return undefined;
  const storage = job.result_summary.storage_verification;
  if (!storage || typeof storage !== 'object' || Array.isArray(storage)) return undefined;
  const record = storage as Record<string, unknown>;
  const folderId = record.remote_folder_id;
  if (record.status !== 'PASS' || typeof folderId !== 'string'
      || !/^[A-Za-z0-9_-]{10,200}$/.test(folderId)) return undefined;
  return `https://drive.google.com/drive/folders/${encodeURIComponent(folderId)}`;
}

function GithubResults({ job, user }: { job: DisplacementJob; user: ResultUser }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (!readGithubResultPackage(job.result_summary)) return null;

  const download = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const link = await getDisplacementResultLink(job.id, user);
      window.location.assign(link.url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not start the result download.');
    } finally {
      setBusy(false);
    }
  };

  return <div className="scientific-results">
    <button type="button" className="investigate-button validated-output-link" disabled={busy || !user} onClick={() => void download()}>
      {busy ? 'Preparing download…' : 'Download results'}
    </button>
    {error && <p role="alert">{error}</p>}
  </div>;
}

export function ScientificResults({ job, user = null }: { job: DisplacementJob; user?: ResultUser }) {
  const folderUrl = driveFolderUrl(job);
  if (folderUrl) {
    return <p className="scientific-results">
      <a className="investigate-button validated-output-link" href={folderUrl} target="_blank" rel="noreferrer">
        Download results
      </a>
    </p>;
  }
  if (job.state === 'succeeded') return <GithubResults job={job} user={user} />;
  return null;
}
