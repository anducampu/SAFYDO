import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { applyAccent } from "../lib/genreColors";
import type { PlayerStatus, SpectrumFrame } from "../types";

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/status`;
}

function currentGenre(status: PlayerStatus | null) {
  return status?.player.saved.meta;
}

export function usePlayerStatus() {
  const [status, setStatus] = useState<PlayerStatus | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  // 20 Hz spectrum frames land here directly — a state update per frame
  // would re-render the whole tree; the Visualizer polls this ref per rAF.
  const bandsRef = useRef<number[] | undefined>(undefined);

  const refresh = useCallback(async () => {
    try {
      const data = await api.status();
      setStatus(data);
      applyAccent(currentGenre(data));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Status unavailable");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;

    const connect = () => {
      if (!alive) return;
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        retryRef.current = 0;
        setError(null);
      };

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as PlayerStatus | SpectrumFrame;
          if ("type" in data && data.type === "spectrum") {
            if (Array.isArray(data.bands)) bandsRef.current = data.bands;
            return;
          }
          const full = data as PlayerStatus;
          if (full.spectrum?.bands) bandsRef.current = full.spectrum.bands;
          setStatus(full);
          // A fresh status is the answer to an earlier failed refresh() too;
          // without this its banner stayed up until the next button press.
          setError(null);
          applyAccent(currentGenre(full));
        } catch {
          /* ignore malformed */
        }
      };

      ws.onclose = () => {
        if (!alive) return;
        setConnected(false);
        // No frames until it reconnects: let the meter fall to rest instead
        // of holding the last level it heard.
        bandsRef.current = [0, 0];
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
        retryRef.current += 1;
        timer = setTimeout(connect, delay);
        void refresh();
      };

      ws.onerror = () => ws.close();
    };

    void refresh();
    connect();

    return () => {
      alive = false;
      clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [refresh]);

  return { status, connected, error, refresh, bandsRef };
}