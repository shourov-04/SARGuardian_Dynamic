import { useEffect, useRef, useState } from 'react';
import type { User } from 'firebase/auth';
import type { DisplacementJob } from '../../src/api/catalog';
import { configureGoogleMaps, importLibrary } from '../../src/googleMaps';
import {
  loadJobResultBlob,
  parseJobResultPackage,
  renderJobOverlay,
  sampleJobMeasurement,
  type JobPixelMeasurement,
  type JobRasterDataset,
} from './jobResultData';
import './langtang.css';

type Props = {
  job: DisplacementJob;
  user: User;
  onHome: () => void;
  onBack: () => void;
  onSignOut: () => void;
};

const TASK_RESULT_DARK_MAP_STYLES: google.maps.MapTypeStyle[] = [
  { elementType: 'geometry', stylers: [{ color: '#18252d' }] },
  { elementType: 'labels.text.stroke', stylers: [{ color: '#18252d' }] },
  { elementType: 'labels.text.fill', stylers: [{ color: '#b9c7cc' }] },
  { featureType: 'administrative', elementType: 'geometry.stroke', stylers: [{ color: '#52636b' }] },
  { featureType: 'administrative.country', elementType: 'labels.text.fill', stylers: [{ color: '#d6e0e3' }] },
  { featureType: 'landscape.natural', elementType: 'geometry', stylers: [{ color: '#203038' }] },
  { featureType: 'poi', elementType: 'geometry', stylers: [{ color: '#253a3c' }] },
  { featureType: 'poi', elementType: 'labels.text.fill', stylers: [{ color: '#90aaa4' }] },
  { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#384b52' }] },
  { featureType: 'road', elementType: 'labels.text.fill', stylers: [{ color: '#aebdc2' }] },
  { featureType: 'transit', elementType: 'geometry', stylers: [{ color: '#2b3c43' }] },
  { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#0b202c' }] },
  { featureType: 'water', elementType: 'labels.text.fill', stylers: [{ color: '#7296a3' }] },
];

function formatValue(value: number, unit: string): string {
  return `${value.toFixed(unit === 'mm' ? 3 : 5)} ${unit}`;
}

export function JobResultExplorerPage({ job, user, onHome, onBack, onSignOut }: Props) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const mapRef = useRef<google.maps.Map | null>(null);
  const aoiOverlay = useRef<google.maps.Polygon | null>(null);
  const imageOverlay = useRef<google.maps.GroundOverlay | null>(null);
  const footprintOverlay = useRef<google.maps.Polygon | null>(null);
  const clickHandler = useRef<(longitude: number, latitude: number) => void>(() => {});
  const [dataset, setDataset] = useState<JobRasterDataset | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loadingStage, setLoadingStage] = useState('Downloading verified result package…');
  const [mapError, setMapError] = useState('');
  const [mapReady, setMapReady] = useState(false);
  const [selectedProductId, setSelectedProductId] = useState('');
  const [selectedBandKey, setSelectedBandKey] = useState('');
  const [opacity, setOpacity] = useState(88);
  const [measurement, setMeasurement] = useState<JobPixelMeasurement | null>(null);
  const [rendering, setRendering] = useState(false);

  const selectedProduct = dataset?.products.find((product) => product.id === selectedProductId) ?? null;
  const selectedBand = selectedProduct?.bands.find((band) => band.key === selectedBandKey) ?? null;

  useEffect(() => {
    let active = true;
    setDataset(null);
    setLoadError('');
    setLoadingStage('Downloading verified result package…');
    void loadJobResultBlob(job, user).then((blob) => {
      if (active) setLoadingStage('Validating and decoding scientific rasters…');
      return parseJobResultPackage(blob, job);
    }).then((loaded) => {
      if (!active) return;
      setDataset(loaded);
      setSelectedProductId(loaded.products[0]?.id ?? '');
      setSelectedBandKey(loaded.products[0]?.bands[0]?.key ?? '');
      setLoadError('');
    }).catch((error: unknown) => {
      if (active) setLoadError(error instanceof Error ? error.message : 'The task result could not be visualized.');
    });
    return () => { active = false; };
  }, [job, user]);

  useEffect(() => {
    if (!mapContainer.current) return undefined;
    if (!configureGoogleMaps()) {
      setMapError('The geographic basemap is unavailable because Google Maps is not configured.');
      return undefined;
    }
    let active = true;
    let listener: google.maps.MapsEventListener | null = null;
    void importLibrary('maps').then((maps) => {
      if (!active || !mapContainer.current) return;
      const map = new maps.Map(mapContainer.current, {
        center: { lat: 0, lng: 0 },
        zoom: 3,
        minZoom: 2,
        maxZoom: 19,
        mapTypeId: 'terrain',
        backgroundColor: '#101b22',
        styles: TASK_RESULT_DARK_MAP_STYLES,
        clickableIcons: false,
        fullscreenControl: true,
        streetViewControl: false,
        mapTypeControl: true,
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
      aoiOverlay.current?.setMap(null);
      imageOverlay.current?.setMap(null);
      footprintOverlay.current?.setMap(null);
      mapRef.current = null;
      setMapReady(false);
    };
  }, []);

  useEffect(() => {
    aoiOverlay.current?.setMap(null);
    aoiOverlay.current = null;
    const map = mapRef.current;
    if (!dataset || !map || !mapReady) return;
    aoiOverlay.current = new google.maps.Polygon({
      map,
      paths: dataset.aoiCoordinates.map(([longitude, latitude]) => ({ lng: longitude, lat: latitude })),
      strokeColor: '#56f1de',
      strokeOpacity: 1,
      strokeWeight: 2.5,
      fillColor: '#56f1de',
      fillOpacity: 0.08,
      clickable: false,
    });
  }, [dataset, mapReady]);

  useEffect(() => {
    imageOverlay.current?.setMap(null);
    imageOverlay.current = null;
    footprintOverlay.current?.setMap(null);
    footprintOverlay.current = null;
    const map = mapRef.current;
    if (!mapReady || !map || !selectedProduct || !selectedBand) return undefined;
    let active = true;
    setRendering(true);
    const timer = window.setTimeout(() => {
      try {
        const dataUrl = renderJobOverlay(selectedProduct, selectedBand);
        if (!active) return;
        const [west, south, east, north] = selectedProduct.boundsWgs84;
        imageOverlay.current = new google.maps.GroundOverlay(
          dataUrl,
          { west, south, east, north },
          { map, opacity: opacity / 100, clickable: false },
        );
        footprintOverlay.current = new google.maps.Polygon({
          map,
          paths: selectedProduct.footprintWgs84.map(([longitude, latitude]) => ({ lng: longitude, lat: latitude })),
          strokeColor: '#ffd37a',
          strokeOpacity: 0.95,
          strokeWeight: 1.5,
          fillOpacity: 0,
          clickable: false,
        });
        map.fitBounds(new google.maps.LatLngBounds({ lat: south, lng: west }, { lat: north, lng: east }), 42);
        setLoadError('');
      } catch (error) {
        if (active) setLoadError(error instanceof Error ? error.message : 'The selected raster could not be rendered.');
      } finally {
        if (active) setRendering(false);
      }
    }, 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
      imageOverlay.current?.setMap(null);
      footprintOverlay.current?.setMap(null);
    };
  }, [mapReady, opacity, selectedBand, selectedProduct]);

  clickHandler.current = (longitude, latitude) => {
    if (!selectedProduct || !selectedBand) return;
    setMeasurement(sampleJobMeasurement(selectedProduct, selectedBand, longitude, latitude));
  };

  useEffect(() => {
    setMeasurement(null);
    if (selectedProduct && !selectedProduct.bands.some((band) => band.key === selectedBandKey)) {
      setSelectedBandKey(selectedProduct.bands[0]?.key ?? '');
    }
  }, [selectedBandKey, selectedProduct]);

  const displayRange = selectedBand?.displayRange;
  const legendLabel = selectedBand?.unit === 'unitless'
    ? '0 to 1 · unitless'
    : displayRange ? `${displayRange[0].toFixed(2)} to ${displayRange[1].toFixed(2)} mm · display clipping` : 'millimetres';

  return <div className="langtang-page">
    <header className="langtang-header">
      <button type="button" className="langtang-brand" onClick={onHome}>SARGuardian <span>Earth in sight</span></button>
      <div><span>{user.displayName || user.email || 'Signed-in researcher'}</span><button type="button" onClick={onBack}>Processing results</button><button type="button" onClick={onSignOut}>Sign out</button></div>
    </header>
    <main className="langtang-layout">
      <aside className="langtang-panel" aria-label="Result raster controls">
        <span className="langtang-kicker">TASK DISPLACEMENT RESULTS</span>
        <h1>Displacement Result Explorer</h1>
        <p>Explore the validated derived rasters retained for this processing task. Offset and LOS bands are radar-coordinate measurements and are not a calibrated horizontal or 3D displacement vector.</p>

        <label>Processed product
          <select value={selectedProductId} disabled={!dataset} onChange={(event) => setSelectedProductId(event.target.value)}>
            {dataset?.products.map((product) => <option key={product.id} value={product.id}>{product.id} · {product.referenceDate} → {product.secondaryDate}</option>)
              ?? <option>Loading task results</option>}
          </select>
        </label>
        {selectedProduct && <p className="langtang-pair-note">{selectedProduct.family} · EPSG:{selectedProduct.epsg} · {selectedProduct.width} × {selectedProduct.height}</p>}

        <label>Measurement band
          <select value={selectedBandKey} disabled={!selectedProduct} onChange={(event) => setSelectedBandKey(event.target.value)}>
            {selectedProduct?.bands.map((band) => <option key={band.key} value={band.key}>{band.label} · {band.unit}</option>)
              ?? <option>Loading bands</option>}
          </select>
        </label>
        <label>Overlay opacity <strong>{opacity}%</strong>
          <input type="range" min="10" max="100" value={opacity} onChange={(event) => setOpacity(Number(event.target.value))} />
        </label>

        <section className="langtang-legend" aria-label="Map legend">
          <div className={selectedBand?.unit === 'unitless' ? 'correlation' : 'offset'} />
          <span>{legendLabel}</span>
          <small>Transparent cells contain no measurement. Enhanced contrast affects display colors only; Pixel Inspector reads the decoded scientific raster.</small>
        </section>

        <section className="langtang-provenance">
          <h2>Task provenance</h2>
          <dl>
            <div><dt>Job</dt><dd>{job.id}</dd></div>
            <div><dt>Source run</dt><dd>{dataset?.runId ?? job.github_run_id ?? 'Loading'}</dd></div>
            <div><dt>Date window</dt><dd>{job.start_date} → {job.end_date}</dd></div>
            <div><dt>Science status</dt><dd>{dataset?.scienceStatus ?? 'Loading'}</dd></div>
          </dl>
          <p>Processing and structural validation do not independently validate geophysical accuracy. This research output is not a hazard warning.</p>
        </section>
      </aside>

      <section className="langtang-map-shell">
        <div ref={mapContainer} className="langtang-map" aria-label="Interactive displacement result map" />
        {mapError && <div className="langtang-map-message" role="alert">{mapError}</div>}
        {loadError && <div className="langtang-data-message" role="alert"><strong>Task visualization unavailable</strong><span>{loadError}</span></div>}
        {!dataset && !loadError && <div className="langtang-data-message" role="status">{loadingStage}</div>}
        {rendering && <div className="langtang-data-message" role="status">Rendering selected scientific raster…</div>}
        <div className="langtang-map-key"><span className="aoi-line" />Requested AOI <span className="footprint-line" />Raster footprint</div>
      </section>

      <aside className="langtang-inspector" aria-live="polite">
        <span>PIXEL INSPECTOR</span>
        <h2>Click the map</h2>
        <p>Values come from the selected task GeoTIFF using its native projected grid. NaN cells remain no-data.</p>
        {measurement && <dl>
          <div><dt>Longitude</dt><dd>{measurement.longitude.toFixed(6)}</dd></div>
          <div><dt>Latitude</dt><dd>{measurement.latitude.toFixed(6)}</dd></div>
          <div><dt>Raster cell</dt><dd>{measurement.column === null ? 'Outside raster' : `column ${measurement.column}, row ${measurement.row}`}</dd></div>
          <div><dt>{selectedBand?.label ?? 'Measurement'}</dt><dd>{measurement.valid && measurement.value !== null ? formatValue(measurement.value, measurement.unit) : 'No measurement'}</dd></div>
          <div><dt>{selectedProduct?.family === 'GUNW' ? 'Coherence' : 'Correlation'}</dt><dd>{measurement.quality === null ? 'No measurement' : measurement.quality.toFixed(5)}</dd></div>
          <div><dt>Cell status</dt><dd>{measurement.valid ? 'Valid measurement' : 'Invalid / no-data'}</dd></div>
        </dl>}
      </aside>
    </main>
  </div>;
}
