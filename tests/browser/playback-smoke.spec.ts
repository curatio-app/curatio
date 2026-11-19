import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';

let fixtureDir = '';
let fixtureDataUrl = '';

test.beforeAll(() => {
  fixtureDir = mkdtempSync(join(tmpdir(), 'curatio-playback-smoke-'));
  const fixture = join(fixtureDir, 'h264-aac.mp4');
  execFileSync('ffmpeg', [
    '-hide_banner',
    '-loglevel', 'error',
    '-f', 'lavfi',
    '-i', 'testsrc=size=160x90:rate=12',
    '-f', 'lavfi',
    '-i', 'sine=frequency=880:sample_rate=22050',
    '-t', '3',
    '-c:v', 'libx264',
    '-profile:v', 'main',
    '-level:v', '3.0',
    '-pix_fmt', 'yuv420p',
    '-preset', 'veryfast',
    '-crf', '34',
    '-c:a', 'aac',
    '-b:a', '24k',
    '-ac', '2',
    '-movflags', '+faststart',
    '-y', fixture,
  ]);
  fixtureDataUrl = `data:video/mp4;base64,${readFileSync(fixture).toString('base64')}`;
});

test.afterAll(() => {
  if (fixtureDir) rmSync(fixtureDir, { recursive: true, force: true });
});

test('H.264/AAC advances, pauses, resumes, and seeks', async ({ page }) => {
  await page.setContent(`
    <video id="video" playsinline preload="auto"></video>
    <button id="play" type="button">Play</button>
    <button id="pause" type="button">Pause</button>
    <script>
      const video = document.querySelector('#video');
      document.querySelector('#play').addEventListener('click', () => video.play());
      document.querySelector('#pause').addEventListener('click', () => video.pause());
    </script>
  `);
  await page.locator('#video').evaluate((video, src) => {
    (video as HTMLVideoElement).src = src;
    (video as HTMLVideoElement).load();
  }, fixtureDataUrl);

  await expect.poll(() => page.locator('#video').evaluate((video) => ({
    readyState: (video as HTMLVideoElement).readyState,
    width: (video as HTMLVideoElement).videoWidth,
  }))).toMatchObject({ readyState: expect.any(Number), width: 160 });

  await page.locator('#play').click();
  await expect.poll(() => page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime)).toBeGreaterThan(0.25);

  await page.locator('#pause').click();
  const pausedAt = await page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime);
  await page.waitForTimeout(350);
  const stillPausedAt = await page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime);
  expect(Math.abs(stillPausedAt - pausedAt)).toBeLessThan(0.12);

  await page.locator('#play').click();
  await expect.poll(() => page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime)).toBeGreaterThan(pausedAt + 0.2);
  await page.locator('#pause').click();

  await page.locator('#video').evaluate(async (video) => {
    const media = video as HTMLVideoElement;
    const seeked = new Promise<void>((resolve) => media.addEventListener('seeked', () => resolve(), { once: true }));
    media.currentTime = 1.5;
    await seeked;
  });
  await expect.poll(() => page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime)).toBeGreaterThan(1.4);
  await page.locator('#play').click();
  await expect.poll(() => page.locator('#video').evaluate((video) => (video as HTMLVideoElement).currentTime)).toBeGreaterThan(1.75);
});
