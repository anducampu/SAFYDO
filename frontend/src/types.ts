export interface Stream {
  id: string;
  name: string;
  url: string;
  genre: string;
  category: string;
  favorite?: boolean;
  disabled?: boolean;
  homepage?: string;
  /** File name under /api/logos/ (content-addressed). */
  logo?: string;
  /** "#rrggbb" chosen in the editor; overrides the genre colour. */
  color?: string;
}

export interface ProbeCandidate {
  url: string;
  name: string | null;
  genre: string | null;
  homepage: string | null;
  format: string | null;
  bitrate: number | null;
  logo_hint?: string | null;
  live?: boolean;
}

export interface ProbeResult {
  streams: ProbeCandidate[];
  title: string | null;
  homepage: string | null;
  error: string | null;
}

export interface LogoBatchStatus {
  running: boolean;
  total: number;
  done: number;
  found: number;
  missing: string[];
  current: string | null;
}

export interface WebAuthStatus {
  enabled: boolean;
  username: string;
  /** On with the built-in password ("safydo"). */
  default_password: boolean;
}

export interface StreamCheckResult {
  id: string;
  name: string;
  url: string;
  ok: boolean;
  reason: string | null;
  disabled: boolean;
}

export interface StreamCheckStatus {
  running: boolean;
  total: number;
  done: number;
  results: StreamCheckResult[];
  checked_at: number | null;
}

export interface DiskUsage {
  label: string;
  path: string;
  total: number;
  used: number;
  free: number;
}

export interface SystemResources {
  cpu: {
    percent: number | null;
    cores: number | null;
    load: number[] | null;
    freq_mhz: number | null;
    freq_max_mhz: number | null;
  };
  temperature_c: number | null;
  memory: {
    total: number;
    available: number;
    used: number;
    swap_total: number;
    swap_used: number;
    backend_rss: number | null;
  };
  disks: DiskUsage[];
  throttle: {
    raw: string;
    under_voltage: boolean;
    throttled: boolean;
    under_voltage_since_boot: boolean;
    throttled_since_boot: boolean;
  } | null;
  uptime_s: number | null;
}

export interface TrackInfo {
  title?: string;
  /** The stream's own name (icy-name). */
  station?: string;
  description?: string;
  bitrate_kbps?: number;
  genres?: string[];
  listeners?: number;
  format?: string;
  sample_rate?: number;
  channels?: number;
}

export interface SpectrumStatus {
  bands: number[];
  freqs?: number[];
  active?: boolean;
}

/** Lightweight 20 Hz WS frame between 1 Hz full-status frames. */
export interface SpectrumFrame {
  type: "spectrum";
  bands: number[];
}

export interface MpvStatus {
  running: boolean;
  idle: boolean;
  paused?: boolean;
  volume?: number;
  title?: string;
  path?: string;
  position?: number;
  duration?: number;
  seekable?: boolean;
  /** Seekable spans in the demuxer cache; on a live stream, the rewind window. */
  cache_ranges?: [number, number][];
  playlist_pos?: number | null;
  playlist_count?: number | null;
  track_info?: TrackInfo;
}

export interface SavedState {
  source?: string;
  url?: string;
  path?: string;
  meta?: {
    name?: string;
    genre?: string;
    filename?: string;
    link_id?: string;
    platform?: string;
    playlist_id?: string;
    color?: string;
  };
  updated_at?: string;
}

// Live radio numbers for the connected link. rssi_golden is deliberately not
// called dBm: on BR/EDR it is dB above/below the receiver's golden receive
// range, so 0 is "comfortably in range" and small negatives are normal.
export interface BtLinkSignal {
  quality?: number | null;
  quality_pct?: number | null;
  rssi_golden?: number | null;
}

// What bt_watchdog.sh last concluded. It deliberately never fails its unit — a
// unit stuck in `failed` would poison the "zero failed units" health check — so
// the UI is where an unrecoverable controller gets to say so.
export interface BtWatchdog {
  rebind_viable?: "yes" | "no" | "unknown";
  reboot_enabled?: boolean;
  verdict?: string;
  at?: string;
  last_event?: string;
  last_event_at?: string;
  wedged?: boolean;
  stuck?: boolean;
}

export interface BtStatus {
  connected?: boolean;
  signal?: BtLinkSignal | null;
  codec?: string;
  bitrate?: string;
  device?: string;
  mac?: string;
  device_codecs?: string[];
  pi_codecs?: string[];
  best_codec?: string;
  is_best_quality?: boolean;
  /** Measured unable to sustain SBC-XQ; runs standard SBC (retry from the UI). */
  xq_denied?: boolean;
  sample_rate?: number | null;
  watchdog?: BtWatchdog;
}

export interface BtDevice {
  mac: string;
  name: string;
  paired: boolean;
  connected: boolean;
  trusted?: boolean;
  primary?: boolean;
  audio?: boolean;
  unnamed?: boolean;
  icon?: string | null;
  rssi?: number | null;
  last_connected?: string | null;
}

export interface AudioCheck {
  ok?: boolean;
  failures?: string[];
  warnings?: string[];
}

export type RadioState =
  | "unsupported"
  | "disabled"
  | "setup_needed"
  | "reboot_required"
  | "not_detected"
  | "ready"
  | "playing";

export interface RadioSignal {
  frequency_khz?: number;
  rssi?: number;
  snr?: number;
  valid?: number;
  multipath?: number;
  rds_sync?: number;
}

export interface RdsData {
  pi: string;
  country_code: string;
  coverage: string;
  programme_ref: number | null;
  callsign: string;
  ps: string;
  rt: string;
  pty: number | null;
  pty_name: string;
  ptyn: string;
  tp: boolean | null;
  ta: boolean | null;
  music: boolean | null;
  di: { stereo?: boolean; artificial_head?: boolean; compressed?: boolean; dynamic_pty?: boolean };
  af: number[];
  af_lf_mf: number[];
  af_announced: number;
  ecc: string;
  language: string;
  pin: string;
  clock: string;
  oda: { group: string; aid: string; name: string }[];
  rt_plus: Record<string, string>;
  eon: { pi: string; ps: string; af: number[]; pty_name: string; ta: boolean | null }[];
  groups: Record<string, number>;
  total_groups: number;
}

export interface RadioSummary {
  state: RadioState;
  listening: boolean;
  frequency_khz: number;
  name: string;
  signal: RadioSignal;
  mono: boolean;
  radio_text: string;
  rds: RdsData | null;
  error: string;
}

export interface RadioStatus extends RadioSummary {
  presets: { name: string; frequency_khz: number }[];
  stations: { frequency_khz: number; rssi: number; snr: number }[];
  scan: { running: boolean; progress: number; found: number };
  band: { min_khz: number; max_khz: number; step_khz: number };
}

export interface PlayerStatus {
  audio_check?: AudioCheck;
  spectrum?: SpectrumStatus;
  radio?: RadioSummary | null;
  player: {
    mpv: MpvStatus;
    saved: SavedState;
    eq: {
      id: string;
      name?: string;
      enabled?: boolean;
      eq_applied?: boolean;
      bands?: number[];
      band_freqs?: number[];
    };
    volume_config: number;
    volume_follow_hardware?: boolean;
    hw_volume?: number | null;
    speaker_ignores_volume?: boolean;
    /** false = connected speaker has no remote (AVRCP) volume at all. */
    speaker_remote_volume?: boolean | null;
    usb_mount: string;
    usb_mounted: boolean;
    live?: boolean;
    rewind_minutes?: number;
    live_lead_s?: number;
    recording?: RecordingStatus;
  };
  bluetooth: BtStatus;
  primary_mac?: string;
}

export interface UsbTrack {
  path: string;
  name: string;
  relative: string;
}

export type FileSourceType = "usb" | "smb" | "nfs";

export interface FileSource {
  id: string;
  label: string;
  type: FileSourceType;
  host?: string;
  share?: string;
  path: string;
  mounted: boolean;
}

export interface FolderEntry {
  name: string;
  rel: string;
}

export interface FileEntry {
  name: string;
  rel: string;
  path: string;
}

export interface BrowseResult {
  source: string;
  path: string;
  parent: string | null;
  dirs: FolderEntry[];
  files: FileEntry[];
}

export interface AddDrivePayload {
  type: "smb" | "nfs";
  host: string;
  share: string;
  label?: string;
  username?: string;
  password?: string;
  domain?: string;
  options?: string;
}

export interface SavedNetwork {
  id: string;
  ssid: string;
}

export interface NetworkLink {
  iface: string | null;
  type: "wifi" | "ethernet" | "usb-gadget" | null;
  gateway: string | null;
  ip4: string | null;
}

export interface NetworkStatus {
  captive_portal: boolean;
  ethernet_priority: boolean;
  hostname?: string;
  mdns?: string;
  ssid?: string | null;
  ip4?: string | null;
  gateway?: string | null;
  dns?: string[];
  wifi_signal?: number | null;
  wifi_dbm?: number | null;
  saved_networks?: SavedNetwork[];
  online?: boolean;
  priority?: unknown;
  link?: NetworkLink;
  wifi_present?: boolean;
  ip_method?: string | null;
}

export interface SourceInfo {
  id: string;
  label: string;
  enabled: boolean;
  needs_config: boolean;
  configured: boolean;
}

export interface DiscoverStation {
  id: string;
  name: string;
  url: string;
  genre: string;
  bitrate?: number | null;
  homepage?: string | null;
  favicon?: string | null;
  country?: string | null;
  source: string;
  /** false: a finite track (Jamendo), true/absent: a broadcast. */
  live?: boolean | null;
}

export interface DeviceConfig {
  hostname: string;
  mdns: string;
  resume_on_boot: boolean;
  rewind_minutes?: number;
  modules?: { links: boolean; files: boolean; recordings: boolean };
  recording?: RecordingSettings;
  /** Back-buffer budget set for what is playing now, and the rate it is based on. */
  rewind_bytes?: number;
  rewind_kbps?: number | null;
  rewind_rate_source?: "measured" | "declared" | "fallback";
  rewind_effective_minutes?: number | null;
  autoconnect_strategy: string;
  wedge_reboot?: boolean;
  wedge_rebind_viable?: "yes" | "no" | "unknown";
  streams_version?: string | null;
  streams_count?: number;
  leds_enabled?: boolean;
  leds_brightness?: number;
  leds_available?: boolean;
  leds_error?: string;
  leds_fitted?: boolean;
  radio_enabled?: boolean;
  radio_state?: RadioState;
  radio_resume_on_boot?: boolean;
  radio_rds_region?: "E" | "U";
  sources?: SourceInfo[];
}

export interface StreamInput {
  name: string;
  url: string;
  genre: string;
  category: string;
  favorite: boolean;
  homepage?: string;
}
export interface WebLink {
  id: string;
  url: string;
  platform: string;
  title: string;
  uploader: string;
  duration?: number | null;
  thumbnail?: string | null;
  playlist_count?: number | null;
  is_live?: boolean;
  added_at: number;
}

export interface WebLinksResponse {
  ytdl: boolean;
  ytdl_version: string | null;
  platforms: Record<string, string>;
  links: WebLink[];
  /** Platforms with saved account cookies (never the content). */
  cookies: Record<string, { saved_at: number }>;
  playlists: WebLinkPlaylist[];
}

export interface WebLinkPlaylist {
  id: string;
  name: string;
  /** Play order. */
  link_ids: string[];
  shuffle: boolean;
  loop: boolean;
  created_at: number;
}

export interface StreamSettings {
  retries: number;
  reconnect_delay_max: number;
  cache_secs: number;
  rebuffer_secs: number;
  prebuffer_on_start: boolean;
  timeout_secs: number;
  user_agent: string;
  output_buffer: number;
}

export interface StreamSettingsResponse extends StreamSettings {
  defaults: StreamSettings;
  /** false on ffmpeg < 7 (Bookworm): the retry limit cannot be enforced. */
  retries_supported: boolean;
}

export type RecordingFormat = "mp3" | "opus" | "flac" | "original";

export interface RecordingSettings {
  format: RecordingFormat;
  mp3_bitrate: number;
  opus_bitrate: number;
  flac_level: number;
  location: "auto" | "usb" | "sd";
  max_minutes: number;
}

export interface RecordingStatus {
  active: boolean;
  error?: string | null;
  label?: string;
  file?: string;
  format?: RecordingFormat;
  quality?: string;
  elapsed?: number;
  bytes?: number;
  max_minutes?: number;
}

export interface Recording {
  id: string;
  name: string;
  file: string;
  location: "usb" | "sd";
  format: string;
  quality?: string | null;
  label?: string | null;
  duration?: number | null;
  bytes: number;
  created_at: number;
}

export interface RecordingsResponse {
  status: RecordingStatus;
  items: Recording[];
  settings: RecordingSettings;
  target_dir: string | null;
  usb_available: boolean;
  space: Record<string, { free: number; total: number }>;
}
