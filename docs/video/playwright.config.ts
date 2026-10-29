import { defineConfig } from '@playwright/test';

/**
 * Real Chrome (the installed Google Chrome, no browser download) against a LOCAL CityPulse and PagerLens on 127.0.0.1.
 * The video is written to out/ (git-ignored). The pace comes from record.spec.ts (narration cues, human cursor glides), not slowMo.
 * Set HEADED=1 to watch it.
 */
export default defineConfig({
  testDir: '.',
  testMatch: 'record.spec.ts',
  outputDir: './out',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 40 * 60_000,
  reporter: [['list']],
  use: {
    channel: 'chrome',
    headless: !process.env.HEADED,
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    screenshot: 'only-on-failure',
    video: { mode: 'on', size: { width: 1920, height: 1080 } },
    bypassCSP: true, // only so the recorder can draw its callout overlays; the apps themselves are unchanged
    launchOptions: { slowMo: Number(process.env.SLOWMO ?? 0) },
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
});
