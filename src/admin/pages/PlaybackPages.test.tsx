import { expect, it } from 'vitest';
import * as PlaybackPages from './PlaybackPages';
import { LiveActivityPage } from './LiveActivityPage';

it('exposes only Live Activity; source integrations moved to the Integrations page', () => {
  expect(PlaybackPages.StreamsPage).toBe(LiveActivityPage);
  expect(Object.keys(PlaybackPages)).toEqual(['StreamsPage']);
});
