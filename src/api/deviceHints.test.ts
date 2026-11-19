import { describe, expect, it } from 'vitest';
import { playbackDeviceHints } from './accountClient';

describe('playbackDeviceHints', () => {
  it('maps the screen to a resolution cap', () => {
    expect(playbackDeviceHints({ screenHeight: 1080, pixelRatio: 2 }).max_resolution).toBe('2160p');
    expect(playbackDeviceHints({ screenHeight: 1080, pixelRatio: 1 }).max_resolution).toBe('1080p');
    expect(playbackDeviceHints({ screenHeight: 720, pixelRatio: 1 }).max_resolution).toBe('720p');
    expect(playbackDeviceHints({}).max_resolution).toBeNull();
  });

  it('only trusts a downlink below the spec ceiling', () => {
    expect(playbackDeviceHints({ downlinkMbps: 4 }).max_bitrate_kbps).toBe(3000);
    expect(playbackDeviceHints({ downlinkMbps: 10 }).max_bitrate_kbps).toBeNull();
    expect(playbackDeviceHints({ downlinkMbps: null }).max_bitrate_kbps).toBeNull();
    expect(playbackDeviceHints({}).audio_channels).toBe(2);
  });
});
