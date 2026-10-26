export const LANGTANG_DATASET = {
  datasetId: 'langtang-40212069747-1',
  sourceRunId: '40212069747',
  sourceRunAttempt: 1,
  sourceRunVersion: '40212069747-1',
  aoiSha256: '51ba8ae3dd6ddd5050fdc8fdd63799fb378eb3fb10acba70e8e144cc04d957e2',
  startDate: '2026-07-20',
  endDate: '2026-08-30',
  manifestPath: '/data/nisar/langtang/40212069747-1/manifest.json',
  aoiWkt: 'POLYGON ((85.463902 28.357602, 85.378759 28.208853, 85.610158 28.184949, 85.670584 28.305019, 85.463902 28.357602))',
  aoiCoordinates: [
    [85.463902, 28.357602],
    [85.378759, 28.208853],
    [85.610158, 28.184949],
    [85.670584, 28.305019],
    [85.463902, 28.357602],
  ] as ReadonlyArray<readonly [longitude: number, latitude: number]>,
} as const;

export const LANGTANG_MANIFEST_PATH = LANGTANG_DATASET.manifestPath;
