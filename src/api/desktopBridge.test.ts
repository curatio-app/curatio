// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepaliveRemuxSession, RemuxUnsupportedError, startRemuxSession, stopRemuxSession } from './accountClient';
import { resetDesktopBridge } from './desktopBridge';

function installBridge(localEngine: boolean, start: (request: unknown) => unknown) {
  const engine = { start: vi.fn(async (request: unknown) => start(request)), keepalive: vi.fn(async () => true), stop: vi.fn(async () => undefined) };
  window.curatioDesktop = { info: async () => ({ localEngine, platform: 'darwin', version: '1', server: null }), engine };
  return engine;
}

afterEach(() => {
  delete window.curatioDesktop;
  resetDesktopBridge();
  vi.restoreAllMocks();
});

const SESSION = { playlist_url: 'curatio-stream://session/local-a/index.m3u8', session: 'local-a', start: 0, mode: 'remux', local: true };

describe('remux sessions in the desktop app', () => {
  it('prepares on this computer instead of the server, keeping surround and passing headers', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const engine = installBridge(true, () => ({ ok: true, value: SESSION }));
    const result = await startRemuxSession('https://cdn/x.mkv', { start: 60, allowHevc: true, surround: false, headers: { Referer: 'https://r/' } });
    expect(result).toEqual(SESSION);
    expect(engine.start).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://cdn/x.mkv', start: 60, allow_hevc: true, surround: true, headers: { Referer: 'https://r/' } }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('keepalive and stop go to the engine for local sessions', async () => {
    const engine = installBridge(true, () => ({ ok: true, value: SESSION }));
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    expect(await keepaliveRemuxSession('local-a')).toBe(true);
    await stopRemuxSession('local-a');
    expect(engine.keepalive).toHaveBeenCalledWith('local-a');
    expect(engine.stop).toHaveBeenCalledWith('local-a');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('maps engine errors to the errors the player already handles', async () => {
    installBridge(true, () => ({ ok: false, code: 'unsupported', message: 'Dolby Vision profile 5 needs full video transcoding' }));
    await expect(startRemuxSession('https://cdn/x.mkv')).rejects.toBeInstanceOf(RemuxUnsupportedError);
  });

  it('uses the server when the engine is not installed', async () => {
    const engine = installBridge(false, () => ({ ok: true, value: SESSION }));
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...SESSION, session: 'srv', local: undefined }), { status: 200 }));
    const result = await startRemuxSession('https://cdn/x.mkv');
    expect(result.session).toBe('srv');
    expect(engine.start).not.toHaveBeenCalled();
    expect(String(fetchSpy.mock.calls[0][0])).toContain('/account/api/playback/remux/start');
  });

  it('asks for the audio track the viewer chose, on this computer and on the server', async () => {
    const engine = installBridge(true, () => ({ ok: true, value: { ...SESSION, audio_index: 2 } }));
    const result = await startRemuxSession('https://cdn/x.mkv', { audioIndex: 2 });
    expect(engine.start).toHaveBeenCalledWith(expect.objectContaining({ audio_index: 2 }));
    expect(result.audio_index).toBe(2);

    delete window.curatioDesktop;
    resetDesktopBridge();
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...SESSION, session: 'srv' }), { status: 200 }));
    await startRemuxSession('https://cdn/x.mkv', { audioIndex: 3 });
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toMatchObject({ audio_index: 3 });
  });

  it('in a browser nothing changes', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ...SESSION, session: 'srv' }), { status: 200 }));
    await startRemuxSession('https://cdn/x.mkv');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});
