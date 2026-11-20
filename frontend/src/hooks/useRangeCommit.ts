import { useEffect, useLayoutEffect, useRef } from "react";

/** Calls [onCommit] when the user lets go of a range input, with its value.
 *
 * Listens to the native `change` event, which React's onChange (really
 * `input`) does not expose: it fires on mouse release even outside the
 * element, on touch end, and on every keyboard step. pointerup alone missed
 * the keyboard and a release beside the slider, leaving a value shown that
 * was never sent. Attach the returned ref to the <input type="range">. */
export function useRangeCommit(onCommit: (value: number) => void) {
  const ref = useRef<HTMLInputElement>(null);
  const latest = useRef(onCommit);
  useLayoutEffect(() => {
    latest.current = onCommit;
  });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const handler = () => latest.current(Number(el.value));
    el.addEventListener("change", handler);
    return () => el.removeEventListener("change", handler);
  }, []);
  return ref;
}
