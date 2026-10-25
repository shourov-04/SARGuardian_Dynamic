import { describe, expect, it } from 'vitest';
import { formatBytes, readGithubResultPackage, readResultProducts, safeHttpsUrl } from '../engine/scientificResults';
import { getDisplacementResultLink } from '../../src/api/catalog';

describe('results produced by the unchanged upstream workflow', () => {
  it('reads the GitHub result package only from a well-formed summary', () => {
    expect(readGithubResultPackage({
      source: 'github_artifact', github_artifact: { name: 'sarguardian-aoi-dashboard-1-1', size_in_bytes: 2048, expires_at: '2026-11-30T00:00:00Z' },
    })).toEqual({ name: 'sarguardian-aoi-dashboard-1-1', sizeBytes: 2048, expiresAt: '2026-11-30T00:00:00Z' });
    expect(readGithubResultPackage({ source: 'github_artifact', github_artifact: { name: '../escape' } })).toBeUndefined();
    expect(readGithubResultPackage({ source: 'other', github_artifact: { name: 'x' } })).toBeUndefined();
    expect(readGithubResultPackage(null)).toBeUndefined();
    expect(readGithubResultPackage({ source: 'github_artifact' })).toBeUndefined();
  });

  it('reads compact product rows and ignores malformed ones', () => {
    expect(readResultProducts({ products: [
      { id: 'GUNW-001', product_type: 'GUNW', status: 'PASS', reference_date: '2026-09-01', secondary_date: '2026-09-13', valid_pixels: 5 },
      { granule_name: 'NISAR_GOFF_example', error: 'bad' },
      'not a product', {}, null,
    ] })).toEqual([
      { id: 'GUNW-001', productType: 'GUNW', status: 'PASS', referenceDate: '2026-09-01', secondaryDate: '2026-09-13', validPixels: 5, error: '' },
      { id: 'NISAR_GOFF_example', productType: '', status: 'UNKNOWN', referenceDate: '', secondaryDate: '', validPixels: undefined, error: 'bad' },
    ]);
    expect(readResultProducts({ products: 'nope' })).toEqual([]);
    expect(readResultProducts(undefined)).toEqual([]);
  });

  it('formats sizes and accepts only credential-free https links', () => {
    expect(formatBytes(undefined)).toBe('unknown size');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(5_242_880)).toBe('5.0 MB');
    expect(formatBytes(214 * 1024 * 1024)).toBe('214 MB');
    expect(safeHttpsUrl('https://github.com/owner/repo/actions/runs/1')).toBe('https://github.com/owner/repo/actions/runs/1');
    expect(safeHttpsUrl('http://github.com/x')).toBeUndefined();
    expect(safeHttpsUrl('https://user:pass@github.com/x')).toBeUndefined();
    expect(safeHttpsUrl('javascript:alert(1)')).toBeUndefined();
    expect(safeHttpsUrl(42)).toBeUndefined();
  });

  it('asks the API for a signed result link with the Firebase token and refuses insecure links', async () => {
    const original = globalThis.fetch;
    const calls: { url: string; authorization: string }[] = [];
    const respond = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    const user = { getIdToken: async () => 'firebase-token' };
    const outcome = async (body: unknown, status = 200) => {
      globalThis.fetch = (async () => respond(body, status)) as typeof fetch;
      try { await getDisplacementResultLink('job-1', user); return 'ok'; } catch (error) { return (error as Error).message; }
    };
    try {
      globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
        calls.push({ url: String(url), authorization: String((init?.headers as Record<string, string>).Authorization) });
        return respond({ url: 'https://storage.example.test/blob?sig=1', filename: 'a.zip', expires_in_seconds: 60 });
      }) as typeof fetch;
      const link = await getDisplacementResultLink(' job-1 ', user);
      expect(link.url).toBe('https://storage.example.test/blob?sig=1');
      expect(calls.length).toBe(1);
      expect(calls[0].url.endsWith('/api/displacement-jobs/job-1/result-link')).toBe(true);
      expect(calls[0].authorization).toBe('Bearer firebase-token');

      expect(await outcome({ url: 'http://storage.example.test/x', filename: 'a.zip', expires_in_seconds: 60 })).toContain('not secure');
      expect(await outcome({ url: 'not a url', filename: 'a.zip', expires_in_seconds: 60 })).toContain('invalid');
      expect(await outcome({ detail: 'The result artifact has expired on GitHub.' }, 410)).toBe('The result artifact has expired on GitHub.');
    } finally {
      globalThis.fetch = original;
    }
  });
});
