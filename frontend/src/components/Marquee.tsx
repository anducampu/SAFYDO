import { useEffect, useRef, useState } from "react";

interface Props {
  text: string;
  className?: string;
  /** px per second scroll speed. */
  speed?: number;
}

/**
 * Single-line text that scrolls infinitely *only when it overflows* its
 * container (otherwise it sits static). Two copies shifted by -50% give a
 * seamless loop; duration scales with width for a constant speed.
 */
export function Marquee({ text, className, speed = 45 }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLSpanElement>(null);
  const [scroll, setScroll] = useState(false);
  const [dur, setDur] = useState(12);

  useEffect(() => {
    const el = wrap.current;
    const sp = inner.current;
    if (!el || !sp) return;
    const check = () => {
      // The text alone: while scrolling, the span also carries a 3rem gap,
      // and counting it kept text scrolling after a wider screen fitted it.
      const pad = Number.parseFloat(getComputedStyle(sp).paddingRight) || 0;
      const textW = sp.scrollWidth - pad;
      const overflow = textW > el.clientWidth + 2;
      setScroll(overflow);
      if (overflow) setDur(Math.max(6, (textW + 48) / speed));
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    ro.observe(sp);
    return () => ro.disconnect();
  }, [text, speed]);

  return (
    <div ref={wrap} className={`overflow-hidden ${className ?? ""}`}>
      <div
        className="flex w-max"
        style={scroll ? { animation: `marquee ${dur}s linear infinite` } : undefined}
      >
        <span ref={inner} className="whitespace-nowrap" style={scroll ? { paddingRight: "3rem" } : undefined}>
          {text}
        </span>
        {scroll && (
          <span className="whitespace-nowrap" style={{ paddingRight: "3rem" }} aria-hidden>
            {text}
          </span>
        )}
      </div>
    </div>
  );
}
