import { useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";

interface Props {
  /** Live 20 Hz [left, right] channel levels (0..1), polled per frame. */
  levelsRef?: MutableRefObject<number[] | undefined>;
  /** Fallback levels from the 1 Hz full-status frame. */
  levels?: number[];
}

const CHANNELS = ["L", "R"] as const;
const ATTACK = 0.6;
const RELEASE = 0.12;
const PEAK_DECAY = 0.02;
const STORE_KEY = "safydo_vu";

function targetLevels(levels?: number[]): [number, number] {
  const l = levels?.[0] ?? 0;
  const r = levels?.[1] ?? levels?.[0] ?? 0;
  return [l, r];
}

function initialOn(): boolean {
  try {
    return localStorage.getItem(STORE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function VuMeter({ levelsRef, levels }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const displayRef = useRef<[number, number]>([0, 0]);
  const peakRef = useRef<[number, number]>([0, 0]);
  const propRef = useRef<[number, number]>(targetLevels(levels));
  const [on, setOn] = useState(initialOn);

  useEffect(() => {
    propRef.current = targetLevels(levels);
  }, [levels]);

  const toggle = () => {
    setOn((v) => {
      const next = !v;
      try {
        localStorage.setItem(STORE_KEY, next ? "on" : "off");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;

    // applyAccent() sets the accent inline on <html>; reading that is free,
    // where getComputedStyle every frame forced a style recalc at 60 fps.
    const accentNow = () =>
      document.documentElement.style.getPropertyValue("--accent").trim() || "#6c5ce7";
    // Canvas roundRect is Safari 16 / Firefox 112 and newer. Thrown from the
    // paused-meter paint inside this effect, it took the whole page down.
    const hasRoundRect = typeof (ctx as { roundRect?: unknown }).roundRect === "function";
    const bar = (x: number, y: number, bw: number, bh: number, r: number) => {
      ctx.beginPath();
      if (hasRoundRect) ctx.roundRect(x, y, bw, bh, r);
      else ctx.rect(x, y, bw, bh);
      ctx.fill();
    };

    const paint = (animate: boolean) => {
      const display = displayRef.current;
      const peak = peakRef.current;
      if (animate) {
        const target = levelsRef?.current?.length
          ? targetLevels(levelsRef.current)
          : propRef.current;
        for (let i = 0; i < 2; i++) {
          const goal = Math.max(0, Math.min(1, target[i]));
          const rate = goal > display[i] ? ATTACK : RELEASE;
          display[i] += (goal - display[i]) * rate;
          peak[i] = Math.max(display[i], peak[i] - PEAK_DECAY);
        }
      }

      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const accent = accentNow();
      const labelW = 16;
      const gap = 10;
      const rowH = (h - gap) / 2;
      const trackX = labelW + 8;
      const trackW = Math.max(1, w - trackX);
      const radius = Math.min(rowH, 8) / 2;

      ctx.font = "600 11px 'IBM Plex Sans', system-ui, sans-serif";
      ctx.textBaseline = "middle";

      for (let i = 0; i < 2; i++) {
        const y = i * (rowH + gap);
        const cy = y + rowH / 2;

        ctx.fillStyle = animate ? "rgba(139,135,152,0.9)" : "rgba(139,135,152,0.4)";
        ctx.textAlign = "left";
        ctx.fillText(CHANNELS[i], 0, cy + 0.5);

        ctx.fillStyle = "rgba(255,255,255,0.06)";
        bar(trackX, y, trackW, rowH, radius);

        if (!animate) continue;

        const level = display[i];
        const fillW = level > 0.001 ? Math.max(level * trackW, radius * 2) : 0;
        if (fillW > 0) {
          const grad = ctx.createLinearGradient(trackX, 0, trackX + trackW, 0);
          grad.addColorStop(0, `${accent}cc`);
          grad.addColorStop(0.6, accent);
          grad.addColorStop(0.82, "#f5b23e");
          grad.addColorStop(1, "#ff5a5f");
          ctx.fillStyle = grad;
          bar(trackX, y, fillW, rowH, radius);
        }
        if (peak[i] > 0.02) {
          const px = Math.min(trackX + peak[i] * trackW, trackX + trackW - 2);
          ctx.fillStyle = peak[i] > 0.82 ? "#ff5a5f" : "rgba(255,255,255,0.85)";
          bar(px, y, 2, rowH, 1);
        }
      }
    };

    if (on) {
      const loop = () => {
        paint(true);
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    } else {
      // Paused: settle bars to rest, then a single static (dimmed) frame.
      displayRef.current = [0, 0];
      peakRef.current = [0, 0];
      paint(false);
    }
    return () => cancelAnimationFrame(raf);
  }, [levelsRef, on]);

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={on}
      title={on ? "Tap to pause the meter" : "Tap to start the meter"}
      className="relative block w-full cursor-pointer rounded-xl"
    >
      <canvas ref={canvasRef} className="block h-16 w-full rounded-xl" aria-label="Stereo level meter" />
      {!on && (
        <span className="pointer-events-none absolute inset-0 flex items-center justify-center text-[10px] uppercase tracking-[0.2em] text-mist">
          meter paused — tap to start
        </span>
      )}
    </button>
  );
}
