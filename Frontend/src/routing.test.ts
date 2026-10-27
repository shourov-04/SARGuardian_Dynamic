import { describe, expect, it, vi } from 'vitest';
import { isLandslideLangtangRoute, isLandslideResultRoute, isLandslideResultsRoute, isLandslideRoute, isLandslideVisualizationRoute, navigatePath, normalizePathname } from './routing';

describe('client-side route recognition', () => {
  it('normalizes query strings and hashes without changing the route', () => {
    expect(normalizePathname('/landslide/results?from=map#summary')).toBe('/landslide/results');
    expect(normalizePathname('')).toBe('/');
  });

  it('recognizes the map and results routes', () => {
    expect(isLandslideRoute('/landslide')).toBe(true);
    expect(isLandslideRoute('/landslide/results')).toBe(true);
    expect(isLandslideResultsRoute('/landslide/results')).toBe(true);
    expect(isLandslideResultsRoute('/landslide/results?from=map')).toBe(true);
    expect(isLandslideResultsRoute('/landslide')).toBe(false);
    expect(isLandslideLangtangRoute('/landslide/langtang?source=map')).toBe(true);
    expect(isLandslideLangtangRoute('/landslide')).toBe(false);
    expect(isLandslideResultRoute('/landslide/result?mode=job')).toBe(true);
    expect(isLandslideVisualizationRoute('/landslide/result?mode=job')).toBe(true);
    expect(isLandslideVisualizationRoute('/landslide/langtang')).toBe(true);
    expect(isLandslideRoute('/')).toBe(false);
  });

  it('pushes a route once and returns the normalized route for React state', () => {
    const pushState = vi.fn();
    expect(navigatePath('/landslide/results?from=map', '/landslide', pushState)).toBe('/landslide/results');
    expect(pushState).toHaveBeenCalledOnce();
    expect(pushState).toHaveBeenCalledWith({}, '', '/landslide/results');

    expect(navigatePath('/landslide/results', '/landslide/results', pushState)).toBe('/landslide/results');
    expect(pushState).toHaveBeenCalledOnce();
  });
});
