export type LangtangBandKey = 'slant_range_offset' | 'along_track_offset' | 'correlation';

export type LangtangAsset = {
  url: string;
  sha256: string;
  bytes: number;
};

export type LangtangBand = {
  source_band: 1 | 2 | 3;
  key: LangtangBandKey;
  label: string;
  unit: 'mm' | 'unitless';
  numeric_dtype: 'float32-le' | 'float64-le';
  numeric: LangtangAsset;
  preview: LangtangAsset & {
    width: number;
    height: number;
    bounds_wgs84: [west: number, south: number, east: number, north: number];
    resampling: 'nearest';
    transparent_nodata: true;
  };
  display_range: [number, number];
  display_palette: string;
  display_only: true;
};

export type LangtangProduct = {
  id: 'GOFF-005' | 'GOFF-009';
  family: 'GOFF';
  maturity: 'PROVISIONAL';
  track: 'D048';
  reference_date: string;
  secondary_date: string;
  pair_semantics: string;
  source_tiff: string;
  source_sha256: string;
  width: number;
  height: number;
  crs: 'EPSG:32645';
  proj4: string;
  transform: [number, number, number, number, number, number];
  bounds_native: [number, number, number, number];
  nodata: 'NaN';
  pixel_sampling: string;
  footprint_wgs84: Array<[longitude: number, latitude: number]>;
  bands: LangtangBand[];
};

export type LangtangManifest = {
  schema_version: 'sarguardian-langtang-web-data/1.0';
  dataset_id: 'langtang-40212069747-1';
  source_run_id: '40212069747';
  source_run_attempt: 1;
  processing_status: 'PASS';
  science_status: 'PASS_WITH_LIMITATIONS';
  maturity: 'PROVISIONAL';
  pixel_stack_status: 'COMPLETED_NO_DETECTION';
  candidate_screening_status: 'SKIPPED_UNSUPPORTED_CUSTOM_AOI';
  historical_observation: true;
  hazard_warning: false;
  aoi: {
    crs: 'EPSG:4326';
    wkt: string;
    coordinates: Array<[longitude: number, latitude: number]>;
  };
  offset_display_range_mm: [number, number];
  offset_display_note: string;
  products: LangtangProduct[];
};

export type PixelMeasurement = {
  longitude: number;
  latitude: number;
  column: number | null;
  row: number | null;
  valid: boolean;
  value: number | null;
  unit: LangtangBand['unit'];
  correlation: number | null;
};
