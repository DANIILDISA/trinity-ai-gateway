// Convert a backend micro-ruble integer into the API-string shape
// ("123.4500") so the existing formatters can consume it. Used when
// reading historical messages from the API where cost is exposed as
// micro-rubles instead of the SSE wire string.

export function micro_to_cost_string(micro: number): string {
  const negative = micro < 0;
  const abs = Math.abs(Math.trunc(micro));
  const whole = Math.floor(abs / 1_000_000);
  const frac = abs % 1_000_000;
  const frac4 = Math.floor(frac / 100)
    .toString()
    .padStart(4, "0");
  return `${negative ? "-" : ""}${whole}.${frac4}`;
}

// Inverse of micro_to_cost_string: parse a backend API ruble string
// ("123.4500" or "-12.34") into a micro-ruble bigint. Tolerates 0–6
// fractional digits and returns 0n for malformed input.
export function api_string_to_micro(s: string): bigint {
  const trimmed = s.trim();
  if (!/^-?\d+(\.\d+)?$/.test(trimmed)) return 0n;
  const negative = trimmed.startsWith("-");
  const body = negative ? trimmed.slice(1) : trimmed;
  const [whole, frac = ""] = body.split(".");
  const fracPadded = (frac + "000000").slice(0, 6);
  const micro = BigInt(whole || "0") * 1_000_000n + BigInt(fracPadded);
  return negative ? -micro : micro;
}
