"use client";

import { forwardRef, useId } from "react";
import type { InputHTMLAttributes } from "react";
import { useTone } from "./Tone";

type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  label?: string;
  error?: string;
};

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, error, id, className = "", ...rest },
  ref,
) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const errorId = error ? `${inputId}-error` : undefined;
  const tone = useTone();
  const dark = tone === "dark";

  const labelClass = dark ? "text-neutral-300" : "text-neutral-700";
  const fieldBase = dark
    ? "bg-neutral-900 text-neutral-100 placeholder:text-neutral-500 focus:border-emerald-600 disabled:bg-neutral-800 disabled:text-neutral-500"
    : "bg-white focus:border-neutral-900 disabled:bg-neutral-100 disabled:text-neutral-500";
  const borderClass = error
    ? dark
      ? "border-red-500"
      : "border-rose-500"
    : dark
      ? "border-neutral-700"
      : "border-neutral-300";
  const errorClass = dark ? "text-red-400" : "text-rose-600";

  return (
    <div className="flex flex-col gap-1">
      {label ? (
        <label htmlFor={inputId} className={`text-sm font-medium ${labelClass}`}>
          {label}
        </label>
      ) : null}
      <input
        ref={ref}
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={errorId}
        className={`h-10 rounded border px-3 text-sm outline-none transition-colors ${fieldBase} ${borderClass} ${className}`}
        {...rest}
      />
      {error ? (
        <p id={errorId} className={`text-xs ${errorClass}`}>
          {error}
        </p>
      ) : null}
    </div>
  );
});

export default Input;
