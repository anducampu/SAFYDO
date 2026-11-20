import { Trash2 } from "lucide-react";

interface Props {
  /** First tap happened: the next one deletes. */
  armed: boolean;
  onClick: () => void;
  /** What is deleted, for screen readers ("recording", "link", "playlist"). */
  what: string;
  disabled?: boolean;
}

/** Two-tap delete. Unarmed it is a quiet bin icon; armed it turns into a red
 * "Delete?" pill, so it is obvious — on a phone too, where a tooltip never
 * shows — that a second tap is needed. Callers disarm after a few seconds. */
export function DeleteButton({ armed, onClick, what, disabled }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={armed ? `Tap again to delete this ${what}` : `Delete ${what}`}
      title={armed ? "Tap again to delete" : "Delete"}
      className={`flex shrink-0 items-center gap-1 rounded-lg transition-colors disabled:opacity-40 ${
        armed
          ? "bg-red-500 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-red-600"
          : "p-2 text-mist hover:bg-white/5 hover:text-red-300"
      }`}
    >
      <Trash2 className={armed ? "h-3.5 w-3.5" : "h-4 w-4"} />
      {armed && "Delete?"}
    </button>
  );
}
