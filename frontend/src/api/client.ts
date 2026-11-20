import type {
  AddDrivePayload,
  BrowseResult,
  BtDevice,
  BtLinkSignal,
  DeviceConfig,
  DiscoverStation,
  FileSource,
  LogoBatchStatus,
  ProbeCandidate,
  ProbeResult,
  StreamCheckStatus,
  SystemResources,
  NetworkStatus,
  PlayerStatus,
  RadioStatus,
  SourceInfo,
  Stream,
  WebLink,
  RecordingSettings,
  RecordingStatus,
  RecordingsResponse,
  StreamSettings,
  StreamSettingsResponse,
  WebLinkPlaylist,
  WebLinksResponse,
  StreamInput,
  UsbTrack,
  WebAuthStatus,
} from "../types";

function errorMessage(body: string, fallback: string): string {
  if (!body) return fallback;
  try {
    const parsed = JSON.parse(body) as { detail?: string | { msg: string }[] };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (Array.isArray(parsed.detail)) {
      return parsed.detail.map((d) => d.msg).join(", ");
    }
  } catch {
    /* not JSON */
  }
  return body;
}

function apiToken(): string | null {
  try {
    return sessionStorage.getItem("safydo_token");
  } catch {
    return null; // storage blocked: every call would otherwise throw here
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { headers, ...rest } = init ?? {};
  const token = apiToken();
  const res = await fetch(path, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Safydo-Token": token } : {}),
      ...headers,
    },
  });
  if (!res.ok) {
    throw new Error(errorMessage(await res.text(), res.statusText));
  }
  return res.json() as Promise<T>;
}

/** Where a station's stored logo is served from. */
export const logoUrl = (name: string) => `/api/logos/${encodeURIComponent(name)}`;

export const api = {
  health: () => request<{ status: string; phase: number }>("/api/health"),
  status: () => request<PlayerStatus>("/api/status"),
  streams: (favorites = false, all = false) =>
    request<Stream[]>(`/api/streams?favorites=${favorites}&all=${all}`),
  playStream: (id: string) =>
    request<PlayerStatus["player"]>(
      `/api/play/stream/${encodeURIComponent(id)}`,
      { method: "POST" }
    ),
  addStream: (input: StreamInput) =>
    request<Stream>("/api/streams", { method: "POST", body: JSON.stringify(input) }),
  replaceStreams: (streams: Stream[]) =>
    request<Stream[]>("/api/streams", { method: "PUT", body: JSON.stringify({ streams }) }),
  probeStream: (input: string) =>
    request<ProbeResult>("/api/streams/probe", { method: "POST", body: JSON.stringify({ input }) }),
  setFavorite: (id: string, favorite: boolean) =>
    request<Stream>(`/api/streams/${encodeURIComponent(id)}/favorite`, {
      method: "POST",
      body: JSON.stringify({ favorite }),
    }),
  findLogo: (
    id: string,
    opts: { url?: string; name?: string; homepage?: string; hint?: string; only_hint?: boolean } = {}
  ) =>
    request<{ stream: Stream; found: boolean; source?: string }>(
      `/api/streams/${encodeURIComponent(id)}/logo`,
      { method: "POST", body: JSON.stringify(opts) }
    ),
  /** [image]: a file (uploaded or pasted) or SVG markup pasted as text. */
  uploadLogo: (id: string, image: Blob | string) => {
    const body = typeof image === "string" ? new Blob([image], { type: "image/svg+xml" }) : image;
    return request<{ stream: Stream; found: boolean; source?: string }>(
      `/api/streams/${encodeURIComponent(id)}/logo/upload`,
      { method: "POST", body, headers: { "Content-Type": body.type || "application/octet-stream" } }
    );
  },
  btSignal: () => request<{ signal: BtLinkSignal | null }>("/api/bt/signal"),
  webAuth: () => request<WebAuthStatus>("/api/auth"),
  setWebAuth: (body: { enabled: boolean; username?: string; password?: string }) =>
    request<WebAuthStatus>("/api/auth", { method: "PUT", body: JSON.stringify(body) }),
  clearLogo: (id: string) =>
    request<Stream>(`/api/streams/${encodeURIComponent(id)}/logo`, { method: "DELETE" }),
  streamCheck: () => request<StreamCheckStatus>("/api/streams/check"),
  startStreamCheck: () => request<StreamCheckStatus>("/api/streams/check", { method: "POST" }),
  streamAlternatives: (id: string) =>
    request<ProbeCandidate[]>(`/api/streams/${encodeURIComponent(id)}/alternatives`),
  patchStream: (
    id: string,
    fields: Partial<Pick<Stream, "name" | "url" | "homepage" | "disabled" | "favorite">>
  ) =>
    request<Stream>(`/api/streams/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(fields) }),
  systemResources: () => request<SystemResources>("/api/system/resources"),
  logoBatch: () => request<LogoBatchStatus>("/api/streams/logos/batch"),
  startLogoBatch: (only_missing: boolean) =>
    request<LogoBatchStatus>("/api/streams/logos/batch", {
      method: "POST",
      body: JSON.stringify({ only_missing }),
    }),
  deleteStream: (id: string) =>
    request<{ ok: boolean; removed: string }>(`/api/streams/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getConfig: () => request<DeviceConfig>("/api/config"),
  radio: () => request<RadioStatus>("/api/radio"),
  radioPlay: (frequency_khz?: number) =>
    request<RadioStatus>("/api/radio/play", { method: "POST", body: JSON.stringify({ frequency_khz }) }),
  radioTune: (frequency_khz: number) =>
    request<RadioStatus>("/api/radio/tune", { method: "POST", body: JSON.stringify({ frequency_khz }) }),
  radioSeek: (up: boolean) =>
    request<RadioStatus>("/api/radio/seek", { method: "POST", body: JSON.stringify({ up }) }),
  radioMono: (enabled: boolean) =>
    request<RadioStatus>("/api/radio/mono", { method: "POST", body: JSON.stringify({ enabled }) }),
  radioStop: () => request<RadioStatus>("/api/radio/stop", { method: "POST" }),
  radioScan: () => request<RadioStatus>("/api/radio/scan", { method: "POST" }),
  radioAddPreset: (name: string, frequency_khz: number) =>
    request<RadioStatus>("/api/radio/presets", { method: "POST", body: JSON.stringify({ name, frequency_khz }) }),
  radioDeletePreset: (frequency_khz: number) =>
    request<RadioStatus>(`/api/radio/presets/${frequency_khz}`, { method: "DELETE" }),
  reboot: () => request<{ status: string }>("/api/system/reboot", { method: "POST" }),
  putConfig: (
    patch: Partial<
      Pick<
        DeviceConfig,
        | "resume_on_boot"
        | "rewind_minutes"
        | "autoconnect_strategy"
        | "wedge_reboot"
        | "leds_enabled"
        | "leds_brightness"
        | "leds_fitted"
        | "radio_enabled"
        | "radio_resume_on_boot"
        | "radio_rds_region"
      >
    > & {
      source_enabled?: Record<string, boolean>;
      jamendo_client_id?: string;
      recording?: Partial<RecordingSettings>;
      modules?: Partial<NonNullable<DeviceConfig["modules"]>>;
    }
  ) => request<DeviceConfig>("/api/config", { method: "PUT", body: JSON.stringify(patch) }),
  sources: () => request<SourceInfo[]>("/api/sources"),
  sourceGenres: (id: string) => request<string[]>(`/api/sources/${encodeURIComponent(id)}/genres`),
  sourceBrowse: (id: string, opts: { genre?: string; q?: string; page?: number } = {}) => {
    const p = new URLSearchParams();
    if (opts.genre) p.set("genre", opts.genre);
    if (opts.q) p.set("q", opts.q);
    if (opts.page) p.set("page", String(opts.page));
    return request<DiscoverStation[]>(`/api/sources/${encodeURIComponent(id)}/browse?${p}`);
  },
  playUrl: (url: string, name?: string, genre?: string, live?: boolean) =>
    request<PlayerStatus["player"]>("/api/play/url", {
      method: "POST",
      body: JSON.stringify({ url, name, genre, ...(live === undefined ? {} : { live }) }),
    }),
  playFile: (path: string) =>
    request<PlayerStatus["player"]>("/api/play/file", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  links: () => request<WebLinksResponse>("/api/links"),
  addLink: (url: string) =>
    request<WebLink>("/api/links", { method: "POST", body: JSON.stringify({ url }) }),
  deleteLink: (id: string) =>
    request<{ ok: boolean }>(`/api/links/${encodeURIComponent(id)}`, { method: "DELETE" }),
  playLink: (id: string) =>
    request<PlayerStatus["player"]>(`/api/play/link/${encodeURIComponent(id)}`, { method: "POST" }),
  setLinkCookies: (platform: string, content: string) =>
    request<WebLinksResponse["cookies"]>(`/api/links/cookies/${encodeURIComponent(platform)}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  deleteLinkCookies: (platform: string) =>
    request<WebLinksResponse["cookies"]>(`/api/links/cookies/${encodeURIComponent(platform)}`, {
      method: "DELETE",
    }),
  createLinkPlaylist: (name: string) =>
    request<WebLinkPlaylist>("/api/links/playlists", { method: "POST", body: JSON.stringify({ name }) }),
  updateLinkPlaylist: (
    id: string,
    patch: Partial<Pick<WebLinkPlaylist, "name" | "link_ids" | "shuffle" | "loop">>
  ) =>
    request<WebLinkPlaylist>(`/api/links/playlists/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  deleteLinkPlaylist: (id: string) =>
    request<{ ok: boolean }>(`/api/links/playlists/${encodeURIComponent(id)}`, { method: "DELETE" }),
  playLinkPlaylist: (id: string, start = 0) =>
    request<PlayerStatus["player"]>(`/api/play/link-playlist/${encodeURIComponent(id)}?start=${start}`, {
      method: "POST",
    }),
  next: () => request("/api/transport/next", { method: "POST" }),
  prev: () => request("/api/transport/prev", { method: "POST" }),
  streamSettings: () => request<StreamSettingsResponse>("/api/stream-settings"),
  putStreamSettings: (patch: Partial<StreamSettings> & { reset?: boolean }) =>
    request<StreamSettingsResponse>("/api/stream-settings", { method: "PUT", body: JSON.stringify(patch) }),
  setHostname: (name: string, force = false) =>
    request<{ hostname: string; mdns: string; url: string }>("/api/system/hostname", {
      method: "POST",
      body: JSON.stringify({ name, force }),
    }),
  recordings: () => request<RecordingsResponse>("/api/recordings"),
  startRecording: () => request<RecordingStatus>("/api/recordings/start", { method: "POST" }),
  stopRecording: () => request<RecordingStatus>("/api/recordings/stop", { method: "POST" }),
  renameRecording: (id: string, name: string) =>
    request<{ id: string; name: string }>(`/api/recordings/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    }),
  deleteRecording: (id: string) =>
    request<{ ok: boolean }>(`/api/recordings/${encodeURIComponent(id)}`, { method: "DELETE" }),
  playRecording: (id: string) =>
    request<PlayerStatus["player"]>(`/api/play/recording/${encodeURIComponent(id)}`, { method: "POST" }),
  recordingDownloadUrl: (id: string) => `/api/recordings/${encodeURIComponent(id)}/download`,
  goLive: () => request("/api/transport/live", { method: "POST" }),
  seek: (position: number) =>
    request("/api/transport/seek", { method: "POST", body: JSON.stringify({ position }) }),
  pause: () => request("/api/transport/pause", { method: "POST" }),
  resume: () => request("/api/transport/resume", { method: "POST" }),
  toggle: () => request("/api/transport/toggle", { method: "POST" }),
  stop: () => request("/api/transport/stop", { method: "POST" }),
  volume: (level: number) =>
    request("/api/volume", {
      method: "PUT",
      body: JSON.stringify({ level }),
    }),
  volumeFollow: (enabled: boolean) =>
    request<PlayerStatus["player"]>("/api/volume/follow", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  usbList: () => request<UsbTrack[]>("/api/usb"),
  usbScan: () => request<UsbTrack[]>("/api/usb/scan", { method: "POST" }),
  filesSources: () => request<FileSource[]>("/api/files/sources"),
  filesBrowse: (source: string, path = "") =>
    request<BrowseResult>(
      `/api/files/browse?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`
    ),
  filesAddDrive: (body: AddDrivePayload) =>
    request<{ ok: boolean; id: string; label: string }>("/api/files/drives", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  filesMountDrive: (id: string) =>
    request<{ ok: boolean; mounted: boolean }>(
      `/api/files/drives/${encodeURIComponent(id)}/mount`,
      { method: "POST" }
    ),
  filesRemoveDrive: (id: string) =>
    request<{ ok: boolean; removed: string }>(
      `/api/files/drives/${encodeURIComponent(id)}`,
      { method: "DELETE" }
    ),
  eqEnabled: (enabled: boolean) =>
    request("/api/eq/enabled", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  eqPreset: (presetId: string) =>
    request(`/api/eq/preset/${encodeURIComponent(presetId)}`, {
      method: "PUT",
    }),
  eqCustom: (bands: number[]) =>
    request("/api/eq/custom", {
      method: "PUT",
      body: JSON.stringify({ bands }),
    }),
  btConnect: (mac?: string) =>
    request("/api/bt/connect", {
      method: "POST",
      body: JSON.stringify(mac ? { mac } : {}),
    }),
  btDevices: () => request<BtDevice[]>("/api/bt/devices"),
  btScan: (seconds = 12) =>
    request<{ ok: boolean; devices: BtDevice[]; seconds: number }>(
      `/api/bt/scan?seconds=${seconds}`,
      { method: "POST" }
    ),
  btScanStart: (seconds = 25) =>
    request<{ ok: boolean; seconds: number; already?: boolean }>(
      `/api/bt/scan/start?seconds=${seconds}`,
      { method: "POST" }
    ),
  btPair: (mac: string) =>
    request("/api/bt/pair", {
      method: "POST",
      body: JSON.stringify({ mac }),
    }),
  btUnpair: (mac: string) =>
    request("/api/bt/unpair", {
      method: "POST",
      body: JSON.stringify({ mac }),
    }),
  btSetPrimary: (mac: string) =>
    request("/api/bt/primary", {
      method: "POST",
      body: JSON.stringify({ mac }),
    }),
  btSelectCodec: (codec: string, mac?: string) =>
    request("/api/bt/codec", {
      method: "POST",
      body: JSON.stringify({ codec, mac }),
    }),
  networkStatus: () => request<NetworkStatus>("/api/network/status"),
  networkAddWifi: (ssid: string, password: string) =>
    request<{ ok: boolean; message?: string }>("/api/network/wifi", {
      method: "POST",
      body: JSON.stringify({ ssid, password }),
    }),
  networkCaptiveStart: () =>
    request<{ ok: boolean; ssid?: string; ip?: string }>(
      "/api/network/captive/start",
      { method: "POST" }
    ),
  networkCaptiveStop: () =>
    request<{ ok: boolean }>("/api/network/captive/stop", { method: "POST" }),
  networkForgetWifi: (id: string) =>
    request<{ ok: boolean; removed: string }>(
      `/api/network/wifi/${encodeURIComponent(id)}`,
      { method: "DELETE" }
    ),
  shutdown: () =>
    request<{ status: string }>("/api/system/shutdown", { method: "POST" }),
  diag: async () => {
    const res = await fetch("/api/system/diag");
    if (!res.ok) throw new Error(errorMessage(await res.text(), "Diagnostics failed"));
    return res.text();
  },
};