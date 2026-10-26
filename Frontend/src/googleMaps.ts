import { importLibrary, setOptions } from '@googlemaps/js-api-loader';

let configured = false;

export function configureGoogleMaps(): boolean {
  const apiKey = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;
  if (!apiKey) return false;
  if (!configured) {
    setOptions({ key: apiKey, v: 'weekly' });
    configured = true;
  }
  return true;
}

export { importLibrary };
