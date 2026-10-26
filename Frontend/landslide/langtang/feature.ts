export function isLangtangExplorerEnabled(): boolean {
  return import.meta.env.VITE_LANGTANG_EXPLORER_ENABLED === 'true';
}
