import { X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { ReactNode } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
  /** Settings-sized: most of a desktop screen, so little needs scrolling. */
  large?: boolean;
  /** Pinned action bar, always visible below the scrollable body. */
  footer?: ReactNode;
}

export function Modal({ open, onClose, title, children, wide, large, footer }: Props) {
  // Callers pass inline arrows (App re-renders at 1 Hz), so read onClose
  // through a ref: the effect then subscribes once per open, not per render.
  const onCloseRef = useRef(onClose);
  useLayoutEffect(() => {
    onCloseRef.current = onClose;
  });
  // Where the press began. A text selection dragged out of an input and
  // released over the backdrop is still a "click" on it, and closing then
  // threw away a half-filled form.
  const pressedBackdrop = useRef(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      // A field that handles Escape itself (cancel a rename) prevents it.
      if (e.key === "Escape" && !e.defaultPrevented) onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  if (!open) return null;

  // Portal to <body> so the modal escapes card stacking contexts (the .glass
  // cards use backdrop-filter, which traps a fixed/z-index child inside them —
  // otherwise later cards like Files paint over the modal's footer).
  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-end justify-center bg-black/60 backdrop-blur-sm sm:items-center sm:p-4"
      onPointerDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && pressedBackdrop.current) onClose();
        pressedBackdrop.current = false;
      }}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className={`glass flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl border-white/10 sm:rounded-2xl ${
          large ? "sm:max-w-5xl lg:h-[85dvh]" : wide ? "sm:max-w-3xl" : "sm:max-w-md"
        }`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-white/5 px-4 py-3">
          <h2 className="font-display text-base font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-mist transition-colors hover:bg-white/5 hover:text-white"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer && <div className="shrink-0 border-t border-white/5 p-4">{footer}</div>}
      </div>
    </div>,
    document.body
  );
}
