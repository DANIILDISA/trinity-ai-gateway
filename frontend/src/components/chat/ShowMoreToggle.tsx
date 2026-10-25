"use client";

// The Show more/less control. Muted by default, brightening when the cursor is
// over the label itself (Claude behaviour). With `hoverReveal` it also stays
// invisible until the surrounding `.group` is hovered (used by thinking);
// without it the control is always visible but dim (used by user messages).
export function ShowMoreToggle({
  expanded,
  onToggle,
  hoverReveal,
}: {
  expanded: boolean;
  onToggle: () => void;
  hoverReveal?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`mt-1.5 text-xs font-medium text-neutral-500 transition-all duration-150 hover:text-neutral-200 ${
        hoverReveal
          ? "opacity-0 focus-visible:opacity-100 group-hover:opacity-100"
          : ""
      }`}
    >
      {expanded ? "Показать меньше" : "Показать больше"}
    </button>
  );
}
