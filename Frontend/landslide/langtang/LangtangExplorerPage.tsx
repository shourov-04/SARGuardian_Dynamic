import { useEffect, useMemo, useRef, useState } from 'react';
import type { User } from 'firebase/auth';
import { configureGoogleMaps, importLibrary } from '../../src/googleMaps';
import { findBand, LANGTANG_MANIFEST_PATH, loadLangtangManifest, NumericRasterCache, resolveAssetUrl, sampleMeasurement } from './data';
import type { LangtangBandKey, LangtangManifest, PixelMeasurement } from './types';
import './langtang.css';

const EXPECTED_AOI: Array<[number, number]> = [
  [85.463902, 28.357602],
  [85.378759, 28.208853],
  [85.610158, 28.184949],
  [85.670584, 28.305019],
  [85.463902, 28.357602],
];

const BAND_OPTIONS: Array<{ key: LangtangBandKey; label: string; unit: string }> = [
  { key: 'slant_range_offset', label: 'Slant-range offset', unit: 'mm' },
  { key: 'along_track_offset', label: 'Along-track offset', unit: 'mm' },
  { key: 'correlation', label: 'Correlation', unit: 'unitless' },
];

type Props = {
  enabled: boolean;
  user: User;
  onHome: () => void;
  onBack: () => void;
  onSignOut: () => void;
};

function formatValue(value: number, unit: string): string {
  const digits = unit === 'mm' ? 3 : 5;
  return `${value.toFixed(digits)} ${unit}`;
}

export function LangtangExplorerPage({ enabled, user, onHome, onBack, onSignOut }: Props) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const imageOverlay = useRef<google.maps.GroundOverlay | null>(null);
  const footprintOverlay = useRef<google.maps.Polygon | null>(null);
  const clickHandler = useRef<(longitude: number, latitude: number) => void>(() => {});
  const rasterCache = useMemo(() => new NumericRasterCache(), []);
  const [manifest, setManifest] = useState<LangtangManifest | null>(null);
  const [manifestError, setManifestError] = useState('');
  const [mapError, setMapError] = useState('');
  const [mapReady, setMapReady] = useState(false);
  const [selectedProductId, setSelectedProductId] = useState<'GOFF-005' | 'GOFF-009'>('GOFF-005');
  const [selectedBand, setSelectedBand] = useState<LangtangBandKey>('slant_range_offset');
  const [opacity, setOpacity] = useState(72);
  const [measurement, setMeasurement] = useState<PixelMeasurement | null>(null);
  const [sampling, setSampling] = useState(false);
  const [samplingError, setSamplingError] = useState('');

  const selectedProduct = manifest?.products.find((product) => product.id === selectedProductId) ?? null;
  const selectedBandDefinition = selectedProduct ? findBand(selectedProduct, selectedBand) : null;

  useEffect(() => {
    if (!enabled) return undefined;
    let active = true;
    void loadLangtangManifest().then((loaded) => {
      if (active) {
        setManifest(loaded);
        setManifestError('');
      }
    }).catch((error: unknown) => {
      if (active) setManifestError(error instanceof Error ? error.message : 'Verified Langtang raster assets are unavailable.');
    });
    return () => { active = false; };
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !mapContainer.current) return undefined;
    if (!configureGoogleMaps()) {
      setMapError('The geographic basemap is unavailable because Google Maps is not configured.');
      return undefined;
    }
    let active = true;
    let listener: google.maps.MapsEventListener | null = null;
    void importLibrary('maps').then((maps) => {
      if (!active || !mapContainer.current) return;
      const map = new maps.Map(mapContainer.current, {
        center: { lat: 28.278, lng: 85.525 },
        zoom: 11,
        minZoom: 7,
        maxZoom: 18,
        mapTypeId: 'terrain',
        clickableIcons: false,
        fullscreenControl: true,
        streetViewControl: false,
        mapTypeControl: true,
      });
      new google.maps.Polygon({
        map,
        paths: EXPECTED_AOI.map(([lng, lat]) => ({ lng, lat })),
        strokeColor: '#56f1de',
        strokeOpacity: 1,
        strokeWeight: 2.5,
        fillColor: '#56f1de',
        fillOpacity: 0.08,
        clickable: false,
      });
      listener = map.addListener('click', (event: google.maps.MapMouseEvent) => {
        if (event.latLng) clickHandler.current(event.latLng.lng(), event.latLng.lat());
      });
      mapRef.current = map;
      setMapReady(true);
      setMapError('');
    }).catch(() => {
      if (active) setMapError('The geographic basemap could not be loaded.');
    });
    return () => {
      active = false;
      listener?.remove();
      imageOverlay.current?.setMap(null);
      footprintOverlay.current?.setMap(null);
      mapRef.current = null;
      setMapReady(false);
    };
  }, [enabled]);

  useEffect(() => {
    imageOverlay.current?.setMap(null);
    imageOverlay.current = null;
    footprintOverlay.current?.setMap(null);
    footprintOverlay.current = null;
    const map = mapRef.current;
    if (!mapReady || !map || !selectedProduct || !selectedBandDefinition) return;
    const [west, south, east, north] = selectedBandDefinition.preview.bounds_wgs84;
    imageOverlay.current = new google.maps.GroundOverlay(
      resolveAssetUrl(LANGTANG_MANIFEST_PATH, selectedBandDefinition.preview.url),
      { west, south, east, north },
      { map, opacity: opacity / 100, clickable: false },
    );
    footprintOverlay.current = new google.maps.Polygon({
      map,
      paths: selectedProduct.footprint_wgs84.map(([lng, lat]) => ({ lng, lat })),
      strokeColor: '#ffd37a',
      strokeOpacity: 0.95,
      strokeWeight: 1.5,
      fillOpacity: 0,
      clickable: false,
    });
    const bounds = new google.maps.LatLngBounds({ lat: south, lng: west }, { lat: north, lng: east });
    map.fitBounds(bounds, 42);
    return () => {
      imageOverlay.current?.setMap(null);
      footprintOverlay.current?.setMap(null);
    };
  }, [mapReady, opacity, selectedBandDefinition, selectedProduct]);

  clickHandler.current = (longitude, latitude) => {
    if (!selectedProduct) return;
    setSampling(true);
    setSamplingError('');
    void sampleMeasurement(
      selectedProduct,
      selectedBand,
      longitude,
      latitude,
      LANGTANG_MANIFEST_PATH,
      rasterCache,
    ).then(setMeasurement).catch((error: unknown) => {
      setMeasurement(null);
      setSamplingError(error instanceof Error ? error.message : 'The numeric measurement could not be read.');
    }).finally(() => setSampling(false));
  };

  useEffect(() => {
    setMeasurement(null);
    setSamplingError('');
  }, [selectedBand, selectedProductId]);

  if (!enabled) {
    return <div className="langtang-page">
      <header className="langtang-header"><button type="button" onClick={onHome}>SARGuardian</button></header>
      <main className="langtang-unavailable">
        <span>LANGTANG SURFACE CHANGE EXPLORER</span>
        <h1>This research view is not enabled.</h1>
        <p>The existing displacement workspace remains available. This historical observation view opens only after its verified raster assets are published.</p>
        <button type="button" onClick={onBack}>Return to displacement map</button>
      </main>
    </div>;
  }

  const displayRange = selectedBandDefinition?.display_range;
  const legendLabel = selectedBand === 'correlation'
    ? '0 to 1 · unitless'
    : displayRange ? `${displayRange[0].toFixed(2)} to ${displayRange[1].toFixed(2)} mm · display clipping` : 'millimetres';

  return <div className="langtang-page">
    <header className="langtang-header">
      <button type="button" className="langtang-brand" onClick={onHome}>SARGuardian <span>Earth in sight</span></button>
      <div><span>{user.displayName || user.email || 'Signed-in researcher'}</span><button type="button" onClick={onBack}>Displacement map</button><button type="button" onClick={onSignOut}>Sign out</button></div>
    </header>
    <main className="langtang-layout">
      <aside className="langtang-panel" aria-label="Observation controls">
        <span className="langtang-kicker">HISTORICAL NISAR OBSERVATION</span>
        <h1>Langtang Surface Change Explorer</h1>
        <p>Explore two provisional D048 GOFF acquisition pairs. Offset bands are radar-coordinate measurements and are not a calibrated horizontal or 3D displacement vector.</p>

        <label>Observation pair
          <select value={selectedProductId} disabled={!manifest} onChange={(event) => setSelectedProductId(event.target.value as typeof selectedProductId)}>
            {manifest?.products.map((product) => <option key={product.id} value={product.id}>{product.id} · {product.reference_date} → {product.secondary_date}</option>)
              ?? <option>Verified assets unavailable</option>}
          </select>
        </label>
        {selectedProduct && <p className="langtang-pair-note">Track {selectedProduct.track} · {selectedProduct.pair_semantics}</p>}

        <label>Measurement band
          <select value={selectedBand} disabled={!manifest} onChange={(event) => setSelectedBand(event.target.value as LangtangBandKey)}>
            {BAND_OPTIONS.map((band) => <option key={band.key} value={band.key}>{band.label} · {band.unit}</option>)}
          </select>
        </label>
        <label>Overlay opacity <strong>{opacity}%</strong>
          <input type="range" min="10" max="100" value={opacity} onChange={(event) => setOpacity(Number(event.target.value))} />
        </label>

        <section className="langtang-legend" aria-label="Map legend">
          <div className={selectedBand === 'correlation' ? 'correlation' : 'offset'} />
          <span>{legendLabel}</span>
          <small>Transparent cells contain no measurement. Map colors are display-only.</small>
        </section>

        <section className="langtang-provenance">
          <h2>Provenance and limitations</h2>
          <dl>
            <div><dt>Source run</dt><dd>{manifest?.source_run_id ?? 'Awaiting verified manifest'}</dd></div>
            <div><dt>Maturity</dt><dd>{manifest?.maturity ?? 'PROVISIONAL (expected)'}</dd></div>
            <div><dt>Science status</dt><dd>{manifest?.science_status ?? 'Unavailable'}</dd></div>
            <div><dt>Pixel stack</dt><dd>{manifest?.pixel_stack_status ?? 'Unavailable'}</dd></div>
          </dl>
          <p>COMPLETED_NO_DETECTION does not show that no disaster occurred. Correlation is a quality indicator, not a probability. This historical research output is not a hazard warning.</p>
        </section>
      </aside>

      <section className="langtang-map-shell">
        <div ref={mapContainer} className="langtang-map" aria-label="Interactive Langtang geographic map" />
        {mapError && <div className="langtang-map-message" role="alert">{mapError}</div>}
        {manifestError && <div className="langtang-data-message" role="alert"><strong>Verified raster assets unavailable</strong><span>{manifestError}</span></div>}
        {!manifest && !manifestError && <div className="langtang-data-message" role="status">Loading verified raster manifest…</div>}
        <div className="langtang-map-key"><span className="aoi-line" />Requested AOI <span className="footprint-line" />Dataset footprint</div>
      </section>

      <aside className="langtang-inspector" aria-live="polite">
        <span>PIXEL INSPECTOR</span>
        <h2>Click the map</h2>
        <p>Values come from the native numeric raster using the nearest EPSG:32645 pixel center. They are never decoded from overlay colors.</p>
        {sampling && <strong>Reading measurement…</strong>}
        {samplingError && <strong className="langtang-error">{samplingError}</strong>}
        {measurement && <dl>
          <div><dt>Longitude</dt><dd>{measurement.longitude.toFixed(6)}</dd></div>
          <div><dt>Latitude</dt><dd>{measurement.latitude.toFixed(6)}</dd></div>
          <div><dt>Raster cell</dt><dd>{measurement.column === null ? 'Outside dataset' : `column ${measurement.column}, row ${measurement.row}`}</dd></div>
          <div><dt>{selectedBandDefinition?.label ?? 'Measurement'}</dt><dd>{measurement.valid && measurement.value !== null ? formatValue(measurement.value, measurement.unit) : 'No measurement'}</dd></div>
          <div><dt>Correlation</dt><dd>{measurement.correlation === null ? 'No measurement' : measurement.correlation.toFixed(5)}</dd></div>
          <div><dt>Cell status</dt><dd>{measurement.valid ? 'Valid measurement' : 'Invalid / no-data'}</dd></div>
        </dl>}
      </aside>
    </main>
  </div>;
}
