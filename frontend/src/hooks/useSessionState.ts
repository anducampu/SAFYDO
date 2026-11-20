import { useCallback, useState } from "react";

/** useState that survives remounts for the rest of this browser tab's session
 * (sessionStorage). Panels remount when you switch tabs or a modal reopens;
 * this keeps a filter or the selected tab where the user left it. */
export function useSessionState<T>(key: string, initial: T): [T, (next: T | ((prev: T) => T)) => void] {
  const storageKey = `safydo.session.${key}`;
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(storageKey);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const resolved = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
        try {
          sessionStorage.setItem(storageKey, JSON.stringify(resolved));
        } catch {
          /* storage blocked: still works until the next remount */
        }
        return resolved;
      });
    },
    [storageKey]
  );
  return [value, set];
}
