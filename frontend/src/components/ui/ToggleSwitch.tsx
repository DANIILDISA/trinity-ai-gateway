"use client";

export interface ToggleSwitchProps {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  /** Accessible label for the switch (no visible text inside). */
  ariaLabel?: string;
}

// Apple-style toggle: a rounded track with a sliding knob. Emerald-600 brand
// accent (matches the "Отправить" button) with a soft glow when ON; neutral
// track when OFF. Pure CSS transitions.
export function ToggleSwitch({
  checked,
  onChange,
  disabled = false,
  ariaLabel,
}: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={onChange}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full outline-none transition-[colors,box-shadow] duration-200 focus-visible:ring-2 focus-visible:ring-emerald-500/40 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none ${
        checked
          ? "bg-emerald-600 shadow-[0_0_8px_2px_rgba(5,150,105,0.55)]"
          : "bg-neutral-700"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform duration-200 ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

// Presentational mirror of ToggleSwitch (same Apple-style track + knob, emerald
// glow when on). Non-interactive (a <span>, aria-hidden) so a WHOLE menu row can
// be a single <button> that toggles — clicking anywhere on the row flips it, with
// no nested-button / double-fire. The row owns role + aria-checked. Shared by the
// composer's model menu and "+" menu so both read identically.
export function ToggleVisual({
  checked,
  disabled = false,
}: {
  checked: boolean;
  disabled?: boolean;
}) {
  return (
    <span
      aria-hidden="true"
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-[colors,box-shadow] duration-200 ${
        disabled ? "opacity-40" : ""
      } ${
        checked
          ? "bg-emerald-600 shadow-[0_0_8px_2px_rgba(5,150,105,0.55)]"
          : "bg-neutral-700"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform duration-200 ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </span>
  );
}

export default ToggleSwitch;
