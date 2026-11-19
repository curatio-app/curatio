// Test-only page: mounts the REAL player for one title, against the mock API in
// scripts/desktop-player-e2e.mjs. Not part of the shipped app (see vite.config.ts
// beside this file).
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../src/styles/global.css';
import { Player } from '../../src/player/Player';
import type { WebMediaItem } from '../../src/api/types';

const params = new URLSearchParams(window.location.search);
const base: WebMediaItem = {
  tmdb_id: 1,
  imdb_id: 'tt0000001',
  media_type: 'movie',
  title: 'Harness Movie',
  // A saved position makes the player resume there (as Continue Watching does).
  position: Number(params.get('position') || 0),
  duration: Number(params.get('duration') || 0),
} as WebMediaItem;

function Harness() {
  const [item, setItem] = useState<WebMediaItem | null>(base);
  if (!item) return <p id="closed">closed</p>;
  return <Player item={item} onClose={() => setItem(null)} onPlay={setItem} />;
}

createRoot(document.getElementById('root')!).render(<Harness />);
