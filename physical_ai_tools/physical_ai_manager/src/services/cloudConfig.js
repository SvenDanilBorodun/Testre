// Single source of truth for the cloud-API URL.
//
// History: apiClient/cloudTrainingApi/workflowApi each captured
// `process.env.REACT_APP_CLOUD_API_URL` at module load with no fallback.
// When the Docker build forgot the build-arg, the bundle inlined `undefined`
// and every fetch became `fetch("undefined/foo")` — which the browser
// resolved as a relative URL → 404 from nginx, with no hint that the build
// was misconfigured. This helper centralises the read + exposes a single
// `isCloudApiConfigured` flag the App banner can show.

export const CLOUD_API_URL = process.env.REACT_APP_CLOUD_API_URL;

export const isCloudApiConfigured = Boolean(CLOUD_API_URL);

if (!isCloudApiConfigured) {
  // eslint-disable-next-line no-console
  console.error(
    '[edubotics] REACT_APP_CLOUD_API_URL missing at build time — every ' +
      'cloud-API call will throw. Rebuild the physical_ai_manager image ' +
      'with this build-arg set.'
  );
}

// Throw at fetch time (NOT at module load) with a clear, German student-
// facing message. Module-load throws white-screen the entire app; this
// only fires when the consumer actually tries to use the cloud API.
export function assertCloudApiConfigured() {
  if (!isCloudApiConfigured) {
    throw new Error(
      'Die Cloud-API-Adresse ist in dieser Build-Version nicht konfiguriert. ' +
        'Bitte das physical-ai-manager-Image mit gültigem ' +
        'REACT_APP_CLOUD_API_URL neu bauen.'
    );
  }
}
