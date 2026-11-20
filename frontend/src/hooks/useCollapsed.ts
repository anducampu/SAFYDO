import { useCallback, useState } from "react";

const PREFIX = "safydo.collapsed.";

/** Per-panel collapsed state, remembered in this browser (a viewer convenience only). */
export function useCollapsed(id: string): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(PREFIX + id) === "1";
    } catch {
      return false;
    }
  });
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(PREFIX + id, next ? "1" : "0");
      } catch {
        /* private mode / blocked storage: state still works for this session */
      }
      return next;
    });
  }, [id]);
  return [collapsed, toggle];
}
