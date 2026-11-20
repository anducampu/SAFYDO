import { Minus, Plus } from "lucide-react";

interface Props {
  collapsed: boolean;
  onToggle: () => void;
  label: string;
}

/** +/- button at the right end of a panel header. Collapsing hides everything
 * but the header (see `section[data-collapsed]` in index.css), so the panel's
 * state — scan results, tabs, typed input — survives a collapse. */
export function CollapseToggle({ collapsed, onToggle, label }: Props) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!collapsed}
      aria-label={`${collapsed ? "Expand" : "Collapse"} ${label}`}
      title={collapsed ? "Expand" : "Collapse"}
      className="shrink-0 rounded-lg p-2 text-mist transition-colors hover:bg-white/5 hover:text-white"
    >
      {collapsed ? <Plus className="h-4 w-4" /> : <Minus className="h-4 w-4" />}
    </button>
  );
}
