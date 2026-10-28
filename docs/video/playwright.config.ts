import { defineConfig } from '@playwright/test';

/**
 * Records the browser scenes of docs/VIDEO-SCRIPT.md in the machine's installed Google Chrome (no browser download).
 * 1920x1080, device scale factor 1, video on. Output goes to docs/video/out/ (git-ignored).
 * Everything runs locally: record.spec.ts starts its own throwaway Postgres and CityPulse on 127.0.0.1.
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'record.spec.ts',
  outputDir: './out',
  workers: 1,
  retries: 0,
  timeout: 30 * 60_000,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    headless: !process.env.HEADED,
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    video: { mode: 'on', size: { width: 1920, height: 1080 } },
    screenshot: 'off',
    trace: 'off',
    actionTimeout: 20_000,
  },
});
