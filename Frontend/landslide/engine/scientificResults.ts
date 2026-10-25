export type ScientificStatus = 'PASS' | 'FAIL' | 'WARNING' | 'NO_DETECTION' | 'NOT_MEASURABLE' | 'INSUFFICIENT_DATA' | 'SKIPPED' | 'SKIPPED_UNSUPPORTED_CUSTOM_AOI';

export type ScientificArtifact = {
  index: number;
  path: string;
  kind: string;
  bytes?: number;
  sha256: string;
  downloadable: boolean;
};

export type ScientificDiagnostic = {
  name: string;
  status: string;
  reason: string;
  observationCount?: number;
  validPixels?: number;
};

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function textValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function numericValue(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

export function scientificStatusLabel(value: unknown): string {
  const labels: Record<ScientificStatus, string> = {
    PASS: 'Processing checks passed',
    FAIL: 'Scientific processing checks failed',
    WARNING: 'Review scientific limitations',
    NO_DETECTION: 'No qualifying motion candidate detected',
    NOT_MEASURABLE: 'Displacement not measurable',
    INSUFFICIENT_DATA: 'Insufficient temporal support',
    SKIPPED: 'Analysis skipped',
    SKIPPED_UNSUPPORTED_CUSTOM_AOI: 'Analysis unavailable for this AOI',
  };
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(labels, value) ? labels[value as ScientificStatus] : 'Scientific status not reported';
}

export function readScientificArtifacts(value: unknown): ScientificArtifact[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((raw, index) => {
    const artifact = asRecord(raw);
    const path = textValue(artifact.path);
    if (!path || path.includes('\\') || path.split('/').some((part) => !part || part === '.' || part === '..')) return [];
    const sha256 = textValue(artifact.sha256);
    return [{
      index, path, kind: textValue(artifact.kind), bytes: numericValue(artifact.bytes), sha256,
      downloadable: Boolean(textValue(artifact.drive_file_id) && /^[a-f0-9]{64}$/i.test(sha256)),
    }];
  });
}

export function readScientificDiagnostics(value: unknown): ScientificDiagnostic[] {
  return Object.entries(asRecord(value)).map(([name, raw]) => {
    const diagnostic = asRecord(raw);
    return {
      name, status: textValue(diagnostic.status),
      reason: textValue(diagnostic.reason) || textValue(diagnostic.message),
      observationCount: numericValue(diagnostic.observation_count),
      validPixels: numericValue(diagnostic.valid_pixels),
    };
  });
}

export function safeLegacyArtifactUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

export function safeHttpsUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

export type GithubResultPackage = {
  name: string;
  sizeBytes?: number;
  expiresAt?: string;
};

/** The result ZIP kept on GitHub for a job run by the unchanged upstream workflow. */
export function readGithubResultPackage(summary: unknown): GithubResultPackage | undefined {
  const record = asRecord(summary);
  if (record.source !== 'github_artifact') return undefined;
  const artifact = asRecord(record.github_artifact);
  const name = textValue(artifact.name);
  if (!/^[A-Za-z0-9._-]{1,200}$/.test(name)) return undefined;
  return { name, sizeBytes: numericValue(artifact.size_in_bytes), expiresAt: textValue(artifact.expires_at) || undefined };
}

export type ResultProduct = {
  id: string;
  productType: string;
  status: string;
  referenceDate: string;
  secondaryDate: string;
  validPixels?: number;
  error: string;
};

export function readResultProducts(summary: unknown): ResultProduct[] {
  const products = asRecord(summary).products;
  if (!Array.isArray(products)) return [];
  return products.flatMap((raw) => {
    const product = asRecord(raw);
    const id = textValue(product.id) || textValue(product.granule_name);
    if (!id) return [];
    return [{
      id, productType: textValue(product.product_type), status: textValue(product.status) || 'UNKNOWN',
      referenceDate: textValue(product.reference_date), secondaryDate: textValue(product.secondary_date),
      validPixels: numericValue(product.valid_pixels), error: textValue(product.error),
    }];
  });
}

export function formatBytes(value: number | undefined): string {
  if (value === undefined || value < 0) return 'unknown size';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${unit === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`;
}
