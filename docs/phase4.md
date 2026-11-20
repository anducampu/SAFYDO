# Phase 4 — React Web UI

Vite + React + Tailwind static build served by nginx. API/WebSocket proxied to FastAPI.

## Build (dev machine)

```bash
cd frontend
npm install
npm run build
```

Deploy `frontend/dist/` with the rest of the project, then on Pi:

```bash
sudo /opt/rpi-audio-player/install.sh --skip-apt
```

## Access

- **UI:** http://safydo.local/
- **API:** proxied at `/api/*`
- **WebSocket:** `ws://safydo.local/ws/status`

## Dev server

```bash
cd frontend && npm run dev
```

Proxies API to Pi at `safydo.local:8000` (edit `vite.config.ts` if needed).

## UI features

- Now Playing + genre-reactive accent colors
- Canvas 5-band visualizer (astats RMS + frontend wobble; not music-reactive per band yet)
- Station browser: search, genre filters, favorites
- Transport controls, volume
- Bluetooth status + reconnect
- EQ toggle + presets (flat / speaker / headphone)
- USB track list + rescan
- Safe shutdown button