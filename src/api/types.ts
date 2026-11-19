export type MediaKind = 'movie' | 'tv' | 'series';

export type ContentScope = 'everything' | 'my_servers';

/** GET /account/api/servers/library: titles on the Plex/Jellyfin servers. */
export interface ServerLibraryResponse {
  items: WebMediaItem[];
  total: number;
  offset: number;
  next_offset: number | null;
  counts: { movies: number; shows: number };
}

export interface Profile {
  id: number;
  display_name: string;
  avatar?: string | null;
  bandwidth_tier?: string | null;
  /** What Home and search show: the whole catalog or only the media servers. */
  content_scope?: ContentScope | null;
  /** Use watch history from the connected Plex/Jellyfin account ("auto": on for the server owner). */
  server_history?: ServerHistory | null;
  user_key: string;
  api_base: string;
  created_at?: string | null;
}

export interface AccountUser {
  id?: number;
  display_name?: string | null;
  email?: string | null;
  user_key?: string | null;
  is_admin?: boolean;
  bandwidth_tier?: string | null;
  active_profile_id?: number | null;
  profiles?: Profile[];
}

export interface ProfilesResponse {
  active_profile_id: number | null;
  profiles: Profile[];
}

export interface ProfilePayload {
  display_name?: string;
  avatar?: string | null;
  bandwidth_tier?: 'low' | 'high' | 'auto';
  content_scope?: ContentScope;
  server_history?: ServerHistory;
}

export type ServerHistory = 'auto' | 'on' | 'off';

/** A title the profile marked "Not for me". */
export interface HiddenTitle {
  tmdb_id: number;
  media_type: 'movie' | 'tv';
  title?: string | null;
  poster_path?: string | null;
  hidden_at?: string | null;
}

export interface WebMediaItem {
  id?: string | number;
  tmdb_id?: number | string | null;
  media_type?: MediaKind | null;
  content_type?: string | null;
  title?: string | null;
  name?: string | null;
  subtitle?: string | null;
  overview?: string | null;
  description?: string | null;
  year?: string | number | null;
  /** Digital home-release date (movies only), yyyy-MM-dd. Rendered as a "Digital" chip. */
  digitalReleaseInfo?: string | null;
  /** Future watchable date; omitted once the date has passed or is unknown. */
  comingSoonDate?: string;
  /** An episode's first air date, yyyy-MM-dd ('' when unknown). */
  air_date?: string | null;
  runtime?: string | number | null;
  runtime_seconds?: number | null;
  runtimeSeconds?: number | null;
  rating?: string | number | null;
  /** Stored MPAA/TV certification such as PG-13, R, or TV-MA. */
  content_rating?: string | null;
  /** Where `rating` came from: "consensus" (MDBList multi-source) or "tmdb". */
  rating_source?: 'consensus' | 'tmdb' | null;
  /**
   * Name of the shelf the hero was drawn from. Only set on `dashboard.hero`.
   * Unrendered: the hero deliberately carries no source label (see `HeroCarousel`).
   */
  hero_source_name?: string | null;
  /** Why the title is in its row ("Because you watched Heat", "Slow-Burn · Heist"). */
  reason?: string | null;
  genres?: string[];
  poster?: string | null;
  poster_path?: string | null;
  backdrop?: string | null;
  backdrop_path?: string | null;
  logo?: string | null;
  logo_path?: string | null;
  imdb_id?: string | null;
  video_id?: string | null;
  season?: number | string | null;
  episode?: number | string | null;
  progress_percent?: number | null;
  progressPercent?: number | null;
  position?: number | null;
  duration?: number | null;
  intro_start?: number | null;
  intro_end?: number | null;
  credits_start?: number | null;
  credits_end?: number | null;
  skip_intro_start?: number | null;
  skip_intro_end?: number | null;
  outro_start?: number | null;
  next_episode?: WebMediaItem | null;
  nextEpisode?: WebMediaItem | null;
  /** Missing (older cached payload) ⇒ not playable. */
  availability?: Availability;
  [key: string]: unknown;
}

/** A tag as the server exposes it. `facet` is the taxonomy group (subgenre,
 * mood, theme, ...) and is what groups chips and the browse sidebar. */
export interface MediaTag {
  id: number;
  name: string;
  facet?: string | null;
  count?: number;
  confidence?: number | null;
}

export interface FacetGroup {
  facet: string;
  tags: MediaTag[];
}

export interface FacetsResponse {
  media_type: MediaKind;
  facets: FacetGroup[];
  total_tags: number;
}

export interface BrowseResponse {
  items: WebMediaItem[];
  total_tags: number;
  has_more: boolean;
}

/** One source's score for a title. `value` is on the source's own scale (so RT
 * renders as 83), `normalized` is the comparable 0-10 figure. */
export interface TitleRating {
  source: string;
  label: string;
  value?: number | null;
  normalized: number;
  votes?: number | null;
}

export interface TitleReview {
  id: string;
  source: 'tmdb' | 'mdblist';
  source_name: string;
  kind: 'user_review' | 'review_excerpt';
  author?: string | null;
  username?: string | null;
  avatar?: string | null;
  rating?: number | null;
  content: string;
  created_at?: string | null;
  updated_at?: string | null;
  url?: string | null;
}

export interface ReviewSummary {
  returned: number;
  total_available: number;
  average_user_rating?: number | null;
  rated_review_count: number;
  sources: Array<'tmdb' | 'mdblist'>;
}

export interface TitleTrailer {
  id: string;
  name: string;
  type: 'trailer' | 'teaser' | 'clip' | 'featurette';
  site: 'youtube' | 'vimeo';
  key: string;
  url: string;
  official: boolean;
  published_at?: string | null;
}

export interface CatalogRow {
  id?: string | number;
  name: string;
  type?: string | null;
  media_type?: MediaKind | null;
  catalog_type?: string | null;
  source?: 'personal' | 'synthesized' | string | null;
  generation_method?: string | null;
  item_count?: number;
  revision?: string | null;
  reason?: {
    kind: string;
    seed?: {
      tmdb_id?: number | null;
      media_type?: MediaKind | null;
      title?: string | null;
    };
    basis_tags?: string[];
  } | null;
  items: WebMediaItem[];
}

export interface DashboardResponse {
  hero?: WebMediaItem | null;
  continue_watching: WebMediaItem[];
  recent_history: WebMediaItem[];
  library: WebMediaItem[];
  catalogs: CatalogRow[];
  movie_catalogs: CatalogRow[];
  series_catalogs: CatalogRow[];
  catalog_sync_pending: boolean;
}

export interface BootstrapResponse {
  account: AccountUser;
  dashboard: {
    hero?: WebMediaItem | null;
    continue_watching: WebMediaItem[];
    recent_history: WebMediaItem[];
    library: WebMediaItem[];
    rows: CatalogRow[];
    catalog_sync_pending: boolean;
  };
}

export interface CastMember {
  name?: string | null;
  character?: string | null;
  profile?: string | null;
}

export interface CrewSummary {
  creators?: string[];
  directors?: string[];
  writers?: string[];
}

export interface EpisodeItem extends WebMediaItem {
  season?: number | string | null;
  episode?: number | string | null;
  still?: string | null;
}

export interface SeasonItem {
  season?: number | string | null;
  title?: string | null;
  name?: string | null;
  episode_count?: number | null;
  episodes?: EpisodeItem[];
}

export interface DetailsResponse {
  item: WebMediaItem;
  seasons?: SeasonItem[];
  cast?: CastMember[];
  crew?: CrewSummary;
  similar?: WebMediaItem[];
  /** Whether `similar` came from our tag vectors or from TMDB's /similar.
   * The rail is labelled from this: "More Like This" is a claim about our own
   * tagging and a popularity-driven fallback should not wear it. */
  similar_source?: 'tags' | 'tmdb';
  tags?: MediaTag[];
  ratings?: TitleRating[];
  reviews?: TitleReview[];
  review_summary?: ReviewSummary;
  content_rating?: string | null;
  content_advisory?: {
    certification?: string | null;
    age_rating?: string | number | null;
    common_sense?: string | number | null;
    source?: 'database' | 'tmdb' | null;
  };
  tagline?: string | null;
  status?: string | null;
  original_language?: string | null;
  homepage?: string | null;
  keywords?: string[];
  trailers?: TitleTrailer[];
  studios?: string[];
  networks?: string[];
  countries?: string[];
  comingSoonDate?: string;
}

/** Playback quality (docs/playback-api.md): the direct file, or a server transcode. */
export type PlaybackQuality = 'original' | 'optimized';

export type PlaybackSourceKind = 'library' | 'addon';

export interface AvailabilitySource {
  provider_id: string;
  label: string;
  kind: PlaybackSourceKind;
}

/** Whether a title can be played here. Missing ⇒ treat as not playable. */
export interface Availability {
  playable: boolean;
  in_library: boolean;
  sources: AvailabilitySource[];
}

export interface PlaybackSubtitle {
  id: string;
  url: string;
  lang?: string | null;
  label?: string | null;
  format?: string | null;
  forced?: boolean;
}

/** One way to play a title (server `PlaybackSource`). Display strings are never parsed. */
export interface PlaybackSource {
  id: string;
  provider_id: string;
  provider_label: string;
  kind: PlaybackSourceKind;
  variant: PlaybackQuality;
  delivery: 'file' | 'hls';
  url: string;
  headers: Record<string, string>;
  label: string;
  detail?: string | null;
  badges: string[];
  container?: string | null;
  video_codec?: string | null;
  audio_codec?: string | null;
  hdr?: string | null;
  resolution?: string | null;
  width?: number | null;
  height?: number | null;
  audio_channels?: number | null;
  languages: string[];
  size_bytes?: number | null;
  bitrate?: number | null;
  duration_seconds?: number | null;
  subtitles: PlaybackSubtitle[];
  route_id: string;
  file_id?: string | null;
  startup_timeout_seconds?: number | null;
}

/** `POST /account/api/playback/sources` body. */
export interface PlaybackSourcesRequest {
  type: 'movie' | 'series';
  imdb_id?: string | null;
  tmdb_id?: number | null;
  season?: number | null;
  episode?: number | null;
  /** null: the profile's saved quality. */
  quality?: PlaybackQuality | null;
  refresh?: boolean;
  viewing_session_id?: string | null;
  title?: string | null;
  profile_id?: number | null;
  /** What this device can take, so Plex/Jellyfin size a transcode to it. */
  max_bitrate_kbps?: number | null;
  max_resolution?: '2160p' | '1440p' | '1080p' | '720p' | '576p' | '480p' | null;
  audio_channels?: number | null;
}

export interface PlaybackSourcesResponse {
  quality: PlaybackQuality;
  availability: Availability;
  /** Best first; clients only filter out what they can't decode. */
  sources: PlaybackSource[];
}

export type FailureCategory = 'network' | 'decode' | 'timeout' | 'auth' | 'not_found' | 'other';

/** `POST /account/api/playback/failure` body (server `FailureReport`). */
export interface FailureReport {
  source_id?: string | null;
  provider_id?: string | null;
  route_id?: string | null;
  file_id?: string | null;
  failure_category?: FailureCategory | null;
  reason?: string | null;
}

export interface PlaybackProviderStatus {
  id: string;
  label: string;
  kind: PlaybackSourceKind;
  status: 'ok' | 'degraded' | 'offline';
}

export interface ApiVersionResponse {
  api_version: number;
  server_version?: string;
  min_clients?: Record<string, string>;
}

export interface ProbeRequestPayload {
  urls: string[];
  imdb_id?: string | null;
  season?: number | null;
  episode?: number | null;
}

export interface ProbeVerdict {
  state: 'passed' | 'failed' | 'indeterminate';
  reason: string | null;
  status: number | null;
  /** Passed probes carry what the host answered with. */
  content_type?: string | null;
  size?: number | null;
  range?: boolean | string | null;
}

export interface ProbeResponse {
  /** Verdict keyed by the probed source URL. */
  results: Record<string, ProbeVerdict>;
  /** True when the media origin itself looks down (not just dead sources). */
  originLooksUnreachable: boolean;
}

export interface ConfirmSourcePayload {
  url: string;
  imdb_id: string;
  season: number;
  episode: number;
}

export interface RemuxSubtitleTrack {
  index: number;
  language?: string | null;
  title?: string | null;
  forced?: boolean;
  default?: boolean;
  /** WebVTT sidecar, written progressively by the server (poll and append). */
  url: string;
}

export interface RemuxStartResponse {
  playlist_url: string;
  session: string;
  duration?: number | null;
  /** Movie time of the first frame (keyframe at/before the requested start): local 0 maps here. */
  start: number;
  requested_start?: number;
  mode: 'remux' | 'transcode' | 'h264-remux';
  video_mode?: 'copy' | 'transcode';
  keepalive_url?: string;
  subtitles?: RemuxSubtitleTrack[];
  /** Which of the file's audio tracks this session carries (a stream index from the probe facts). */
  audio_index?: number | null;
  /** Prepared on this computer by the desktop app's engine, not by the server. */
  local?: boolean;
  /** Desktop engine: how the audio was carried ('copy', lossless 'flac', or 'aac'). */
  audio_mode?: 'copy' | 'flac' | 'aac' | null;
}

// ---- Server ffprobe facts (POST /account/api/playback/inspect) ----

export interface MediaVideoFacts {
  index: number;
  codec: string;
  profile?: string | null;
  level?: number | null;
  pix_fmt?: string | null;
  bit_depth?: number | null;
  width?: number | null;
  height?: number | null;
  frame_rate?: number | null;
  dynamic_range?: 'sdr' | 'hdr10' | 'hlg' | 'dv' | string | null;
  dv_profile?: number | null;
  /** RFC 6381 string, e.g. avc1.640028 / hvc1.2.4.L153.B0, or null when unknown. */
  codec_string?: string | null;
}

export interface MediaAudioFacts {
  index: number;
  codec: string;
  profile?: string | null;
  channels?: number | null;
  language?: string | null;
  title?: string | null;
  default?: boolean;
  /** mp4a.40.2, ec-3, ac-3, dtsc, mlpa, opus, flac… or null. */
  codec_string?: string | null;
}

export interface MediaSubtitleFacts {
  index: number;
  codec: string;
  language?: string | null;
  title?: string | null;
  forced?: boolean;
  default?: boolean;
  text?: boolean;
}

export interface MediaFacts {
  container: 'matroska' | 'mp4' | 'mpegts' | 'avi' | 'hls' | string;
  duration?: number | null;
  bitrate?: number | null;
  size?: number | null;
  start_time?: number | null;
  video: MediaVideoFacts | null;
  audio: MediaAudioFacts[];
  subtitles: MediaSubtitleFacts[];
}

export type InspectResult =
  | { state: 'ok'; media: MediaFacts }
  | { state: 'failed'; reason: string }
  | { state: 'indeterminate'; reason: string };

export interface InspectResponse {
  results: Record<string, InspectResult>;
}

export interface SegmentMarker {
  type?: string | null;
  start?: number | null;
  end?: number | null;
  start_sec?: number | null;
  end_sec?: number | null;
  startSeconds?: number | null;
  endSeconds?: number | null;
}

export interface SegmentsResponse {
  video_id?: string;
  status?: 'ready' | 'learning' | 'pending' | 'not_found' | 'unavailable' | string;
  segments?: SegmentMarker[];
  fingerprint_status?: 'unavailable' | 'pending' | 'partial' | 'complete';
  retry_after_sec?: number;
}

/** One completed item from `/account/api/watched` (TV rows carry season/episode). */
export interface WatchedEntry {
  tmdb_id?: number | null;
  imdb_id?: string | null;
  media_type: 'movie' | 'tv';
  season?: number | null;
  episode?: number | null;
  title?: string | null;
  watched_at?: string | null;
}

export interface WatchedResponse {
  items: WatchedEntry[];
}

export interface ScrobblePayload {
  tmdb_id?: number | null;
  imdb_id?: string | null;
  media_type: 'movie' | 'tv';
  season?: number | null;
  episode?: number | null;
  title?: string | null;
}

export interface SegmentFeedbackPayload {
  segment_type: 'intro' | 'credits';
  feedback: 'correct' | 'wrong';
  adjusted_start?: number | null;
  adjusted_end?: number | null;
}

export interface PlaybackProgressPayload {
  tmdb_id?: number | string | null;
  media_type?: MediaKind | null;
  season?: number | string | null;
  episode?: number | string | null;
  position: number;
  duration: number;
  title: string;
}

export interface LibraryAddPayload {
  tmdb_id?: number | string | null;
  imdb_id?: string | null;
  media_type?: MediaKind | null;
  title: string;
  poster_path?: string | null;
  year?: string | number | null;
}
