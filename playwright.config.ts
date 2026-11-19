import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  timeout: 30_000,
  globalTimeout: 180_000,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 3 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    actionTimeout: 10_000,
    launchOptions: { timeout: 15_000 },
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'firefox', use: { browserName: 'firefox' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
});
