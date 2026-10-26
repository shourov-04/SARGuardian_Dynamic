import { renderToStaticMarkup } from 'react-dom/server';
import type { User } from 'firebase/auth';
import { describe, expect, it } from 'vitest';
import { LangtangExplorerPage } from '../langtang/LangtangExplorerPage';

const user = { displayName: 'Researcher', email: 'researcher@example.test' } as User;

describe('Langtang explorer safety states', () => {
  it('stays closed when the feature flag is off', () => {
    const html = renderToStaticMarkup(<LangtangExplorerPage enabled={false} user={user} onHome={() => {}} onBack={() => {}} onSignOut={() => {}} />);
    expect(html).toContain('This research view is not enabled.');
    expect(html).not.toContain('Click the map');
  });

  it('labels the historical scientific limitations without inventing measurements', () => {
    const html = renderToStaticMarkup(<LangtangExplorerPage enabled={true} user={user} onHome={() => {}} onBack={() => {}} backLabel="Processing results" onSignOut={() => {}} />);
    expect(html).toContain('Langtang Surface Change Explorer');
    expect(html).toContain('Processing results');
    expect(html).toContain('not a calibrated horizontal or 3D displacement vector');
    expect(html).toContain('not a hazard warning');
    expect(html).toContain('no measurement');
    expect(html).not.toContain('predicted landslide');
    expect(html).not.toContain('90% risk');
  });
});
