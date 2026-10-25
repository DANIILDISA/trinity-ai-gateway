const RUBLE = "₽";
const NBSP = " ";

function addThousands(intStr: string): string {
  return intStr.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
}

function microFromApiString(s: string): bigint {
  const trimmed = s.trim();
  const negative = trimmed.startsWith("-");
  const body = negative ? trimmed.slice(1) : trimmed;
  const [whole, frac = ""] = body.split(".");
  const fracPadded = (frac + "000000").slice(0, 6);
  const micro = BigInt(whole || "0") * 1_000_000n + BigInt(fracPadded);
  return negative ? -micro : micro;
}

function formatRubFromMicro(micro: bigint): string {
  const negative = micro < 0n;
  const abs = negative ? -micro : micro;
  const MICRO = 1_000_000n;
  const whole = abs / MICRO;
  const remainder = abs % MICRO;

  // 4-decimal-place fraction (integer 0..9999)
  const frac4 = Number(remainder / 100n);
  let fracStr = frac4.toString().padStart(4, "0");
  fracStr = fracStr.replace(/0+$/, "");
  if (fracStr.length < 2) fracStr = fracStr.padEnd(2, "0");

  return `${negative ? "-" : ""}${addThousands(whole.toString())}.${fracStr}`;
}

// Round micro-rubles to kopecks (2dp), half-up. Used for balance and any
// rubles-facing display that should never show sub-kopeck noise (BUG 5).
function formatRub2dp(micro: bigint): string {
  const negative = micro < 0n;
  const abs = negative ? -micro : micro;
  const kopecks = (abs + 5_000n) / 10_000n; // 1 kopeck = 10_000 micro
  const whole = kopecks / 100n;
  const frac = kopecks % 100n;
  return `${negative ? "-" : ""}${addThousands(whole.toString())}.${frac
    .toString()
    .padStart(2, "0")}`;
}

export function formatBalance(apiString: string): string {
  return `${formatRub2dp(microFromApiString(apiString))} ${RUBLE}`;
}

export function formatMicroRub(microRub: number): string {
  const big = BigInt(Math.trunc(microRub));
  return `${formatRubFromMicro(big)} ${RUBLE}`;
}

export function formatCost(apiString: string): string {
  return `${formatRubFromMicro(microFromApiString(apiString))} ${RUBLE}`;
}

export function formatTokenCount(n: number): string {
  return addThousands(Math.trunc(n).toString());
}

// Friendly model label from a raw id: drop the provider prefix (text after the
// last "/"), turn hyphens into spaces, and title-case each word's first letter.
// "anthropic/claude-opus-4.5" → "Claude Opus 4.5"; "openai/gpt-oss-20b" →
// "Gpt Oss 20b". Mirrors the ChatSidebar recent-list display exactly.
export function formatModelName(id: string): string {
  const last = id.split("/").pop() ?? id;
  return last
    .replace(/-/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

// Compact context-window label: 1М / 200К / 128К токенов; exact below 1000.
export function formatContextWindow(tokens: number): string {
  const n = Math.trunc(Number.isFinite(tokens) ? tokens : 0);
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    const s = Number.isInteger(m) ? m.toString() : m.toFixed(1).replace(/\.0$/, "");
    return `${s}М токенов`;
  }
  if (n >= 1_000) {
    return `${Math.round(n / 1_000)}К токенов`;
  }
  return `${n} токенов`;
}

const RUSSIAN_MONTHS_SHORT = [
  "янв",
  "фев",
  "мар",
  "апр",
  "май",
  "июн",
  "июл",
  "авг",
  "сен",
  "окт",
  "ноя",
  "дек",
] as const;

export function formatDateTime(isoString: string): string {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  const day = d.getDate();
  const month = RUSSIAN_MONTHS_SHORT[d.getMonth()];
  const year = d.getFullYear();
  const hh = d.getHours().toString().padStart(2, "0");
  const mm = d.getMinutes().toString().padStart(2, "0");
  return `${day} ${month} ${year}, ${hh}:${mm}`;
}

function formatRub4dp(micro: bigint): string {
  const negative = micro < 0n;
  const abs = negative ? -micro : micro;
  const MICRO = 1_000_000n;
  const whole = abs / MICRO;
  const frac4 = Number((abs % MICRO) / 100n)
    .toString()
    .padStart(4, "0");
  return `${negative ? "-" : ""}${addThousands(whole.toString())}.${frac4}`;
}

// Transaction amount display (BUG 10):
//   debit (spending)  → no sign      ("99.4000 ₽")
//   credit / refund   → "+" prefix   ("+500.0000 ₽")
//   adjustment        → sign by direction ("+X" / "−X")
// Color is applied by the caller; this only governs the sign glyph.
export function formatTransactionAmount(
  amount: string,
  type: TransactionTypeForFormatting,
): string {
  const trimmed = amount.trim();
  const isNegative = trimmed.startsWith("-");
  const positive = isNegative ? trimmed.slice(1) : trimmed;
  const [whole, frac = ""] = positive.split(".");
  const fracPadded = (frac + "000000").slice(0, 6);
  const micro = BigInt(whole || "0") * 1_000_000n + BigInt(fracPadded);
  const body = formatRub4dp(micro);

  if (type === "debit") return `${body} ${RUBLE}`;
  if (type === "credit" || type === "refund") return `+${body} ${RUBLE}`;
  // adjustment
  return `${isNegative ? "−" : "+"}${body} ${RUBLE}`;
}

// Local type alias — narrower than the full TransactionType, since formatters
// only branch on debit vs credit.
type TransactionTypeForFormatting = "debit" | "credit" | string;

export function formatPrice(apiString: string): string {
  const micro = microFromApiString(apiString);
  return `${formatRub4dp(micro)} ${RUBLE}`;
}

// Tariff display (BUG 11): round to kopecks and drop a trailing ".00" so whole
// prices read "391 ₽" while meaningful kopecks stay ("391.50 ₽").
export function formatPriceShort(apiString: string): string {
  const micro = microFromApiString(apiString);
  const negative = micro < 0n;
  const abs = negative ? -micro : micro;
  const kopecks = (abs + 5_000n) / 10_000n;
  const whole = kopecks / 100n;
  const frac = kopecks % 100n;
  let body = addThousands(whole.toString());
  if (frac !== 0n) body += `.${frac.toString().padStart(2, "0")}`;
  return `${negative ? "-" : ""}${body} ${RUBLE}`;
}

// Russian plural for "день": 1 → день, 2-4 → дня, 5-20 → дней,
// then repeats by decade.
function pluralDays(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return "дней";
  const mod10 = n % 10;
  if (mod10 === 1) return "день";
  if (mod10 >= 2 && mod10 <= 4) return "дня";
  return "дней";
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

export function formatRelativeDate(
  isoString: string,
  now: Date = new Date(),
): string {
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  const today = startOfDay(now);
  const that = startOfDay(d);
  const diffDays = Math.round(
    (today.getTime() - that.getTime()) / 86_400_000,
  );

  if (diffDays === 0) return "сегодня";
  if (diffDays === 1) return "вчера";
  if (diffDays >= 2 && diffDays <= 6) return `${diffDays} ${pluralDays(diffDays)} назад`;

  const month = RUSSIAN_MONTHS_SHORT[d.getMonth()];
  const day = d.getDate();
  if (d.getFullYear() === now.getFullYear()) {
    return `${day} ${month}`;
  }
  return `${day} ${month} ${d.getFullYear()}`;
}
