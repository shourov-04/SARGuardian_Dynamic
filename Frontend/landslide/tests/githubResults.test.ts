import { describe, expect, it } from 'vitest';
import { readGithubResultPackage } from '../engine/scientificResults';
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
