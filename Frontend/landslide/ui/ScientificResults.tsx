import { useState } from 'react';
import type { User } from 'firebase/auth';
import { getDisplacementResultLink, type DisplacementJob } from '../../src/api/catalog';
import { formatBytes, readGithubResultPackage, readResultProducts, scientificStatusLabel } from '../engine/scientificResults';

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

/** Output of the unchanged upstream workflow: a summary, the product table and one ZIP download. */
function GithubResults({ job, user }: { job: DisplacementJob; user: ResultUser }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const result = readGithubResultPackage(job.result_summary);
  const products = readResultProducts(job.result_summary);
  if (!result) return null;
  const expires = result.expiresAt ? new Date(result.expiresAt) : null;
  const expiresText = expires && !Number.isNaN(expires.getTime()) ? ` · available until ${expires.toLocaleDateString()}` : '';

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

  return <div className="scientific-results github-results">
    <h2>Results</h2>
    <p><b>{scientificStatusLabel(job.result_summary?.scientific_status)}</b> · {products.length} product{products.length === 1 ? '' : 's'} processed</p>
    <p>
      <button type="button" className="investigate-button validated-output-link" disabled={busy || !user} onClick={() => void download()}>
        {busy ? 'Preparing download…' : 'Download results (ZIP)'}
      </button>
    </p>
    <p className="catalog-notice">
      {`${result.name}.zip · ${formatBytes(result.sizeBytes)}${expiresText}. Contains the dashboard (index.html), the derived results ZIP, the run summary and the product table.`}
    </p>
    {error && <p role="alert">{error}</p>}
    {products.length > 0 && <details className="github-results-products">
      <summary>Processed products ({products.length})</summary>
      <div className="github-results-table-wrap">
        <table>
          <thead><tr><th>Product</th><th>Type</th><th>Acquisitions</th><th>Status</th><th>Valid pixels</th></tr></thead>
          <tbody>
            {products.map((product, index) => <tr key={`${product.id}-${index}`} title={product.error || undefined}>
              <td>{product.id}</td>
              <td>{product.productType}</td>
              <td>{product.referenceDate || '—'} → {product.secondaryDate || '—'}</td>
              <td>{product.status}</td>
              <td>{product.validPixels ?? '—'}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
    </details>}
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
  if (job.state === 'succeeded' && readGithubResultPackage(job.result_summary)) return <GithubResults job={job} user={user} />;
  return null;
}
