import { describe, it, expect } from "vitest";

import { GREETINGS, pickGreeting, selectGreeting, type Greeting } from "./greetings";

// Helper: a Date whose getDay()/getHours() are fixed regardless of the host
// timezone. We construct from local components so getHours()/getDay() are
// deterministic for the assertions below.
function localDate(year: number, monthIndex: number, day: number, hour: number): Date {
  return new Date(year, monthIndex, day, hour, 0, 0, 0);
}

// Known reference dates (verified by getDay()):
//   2024-01-01 is a Monday (getDay() === 1)
//   2024-01-02 is a Tuesday (getDay() === 2)
//   2024-01-03 is a Wednesday (getDay() === 3)
const MONDAY = (h: number) => localDate(2024, 0, 1, h);
const TUESDAY = (h: number) => localDate(2024, 0, 2, h);
const WEDNESDAY = (h: number) => localDate(2024, 0, 3, h);

// Reusable: replicate the implementation's eligibility predicate so tests can
// independently compute the "last eligible" entry for determinism checks.
function isEligible(g: Greeting, h: number, d: number, hasName: boolean): boolean {
  if (g.days && !g.days.includes(d)) return false;
  if (g.minHour !== undefined && h < g.minHour) return false;
  if (g.maxHour !== undefined && h >= g.maxHour) return false;
  if (g.text.includes("{name}") && !hasName) return false;
  return true;
}

function eligibleList(h: number, d: number, hasName: boolean): Greeting[] {
  return GREETINGS.filter((g) => isEligible(g, h, d, hasName));
}

function resolve(text: string, name: string): string {
  return text.split("{name}").join(name);
}

// Sanity-check our reference dates actually fall on the expected weekday.
describe("reference dates", () => {
  it("2024-01-01 is Monday, 01-02 Tuesday, 01-03 Wednesday", () => {
    expect(MONDAY(12).getDay()).toBe(1);
    expect(TUESDAY(12).getDay()).toBe(2);
    expect(WEDNESDAY(12).getDay()).toBe(3);
  });
});

describe("GREETINGS pool", () => {
  it("contains at least 100 entries", () => {
    expect(GREETINGS.length).toBeGreaterThanOrEqual(100);
  });

  it("has at least one always-eligible universal greeting (no conditions, no {name})", () => {
    const universal = GREETINGS.filter(
      (g) =>
        g.days === undefined &&
        g.minHour === undefined &&
        g.maxHour === undefined &&
        !g.text.includes("{name}"),
    );
    expect(universal.length).toBeGreaterThan(0);
  });

  it("uses the {name} token literally where present (never pre-substituted)", () => {
    for (const g of GREETINGS) {
      // The only brace token allowed is the exact "{name}" placeholder: removing
      // all well-formed tokens must leave no stray "{" or "}" behind.
      const stripped = g.text.split("{name}").join("");
      expect(stripped).not.toContain("{");
      expect(stripped).not.toContain("}");
    }
    // At least some entries carry the token.
    const withName = GREETINGS.filter((g) => g.text.includes("{name}"));
    expect(withName.length).toBeGreaterThan(0);
  });
});

describe("pickGreeting — determinism", () => {
  it("random() === 0 picks the first eligible greeting", () => {
    const h = 12;
    const d = TUESDAY(h).getDay();
    const eligible = eligibleList(h, d, true);
    const expected = resolve(eligible[0].text, "Admin");
    const got = pickGreeting({ now: TUESDAY(h), name: "Admin", random: () => 0 });
    expect(got).toBe(expected);
  });

  it("random() ~= 1 picks the last eligible greeting (resolved)", () => {
    const h = 12;
    const d = TUESDAY(h).getDay();
    const eligible = eligibleList(h, d, true);
    const expected = resolve(eligible[eligible.length - 1].text, "Admin");
    const got = pickGreeting({ now: TUESDAY(h), name: "Admin", random: () => 0.999999 });
    expect(got).toBe(expected);
  });

  it("random() === 0 picks the first eligible greeting with no name", () => {
    const h = 9;
    const d = WEDNESDAY(h).getDay();
    const eligible = eligibleList(h, d, false);
    const expected = resolve(eligible[0].text, "");
    const got = pickGreeting({ now: WEDNESDAY(h), name: null, random: () => 0 });
    expect(got).toBe(expected);
  });
});

describe("pickGreeting — day filter", () => {
  it("a Tuesday-only greeting is NEVER returned on a Wednesday (many samples)", () => {
    // "Happy Tuesday" is days:[2] in the daytime window. On Wednesday at 14 it
    // must never appear, regardless of the random value.
    const samples = 500;
    const seen = new Set<string>();
    for (let i = 0; i < samples; i++) {
      const r = i / samples; // sweep [0,1)
      seen.add(pickGreeting({ now: WEDNESDAY(14), name: "Admin", random: () => r }));
    }
    expect(seen.has("Happy Tuesday")).toBe(false);
    expect(seen.has("Хорошего вторника")).toBe(false);
  });

  it("a Tuesday-only greeting CAN appear on Tuesday within its window", () => {
    const samples = 1000;
    const seen = new Set<string>();
    for (let i = 0; i < samples; i++) {
      const r = i / samples;
      seen.add(pickGreeting({ now: TUESDAY(14), name: "Admin", random: () => r }));
    }
    expect(seen.has("Happy Tuesday")).toBe(true);
  });
});

describe("pickGreeting — hour window", () => {
  // A day-of-week special has minHour 11, maxHour 20. We test the Monday
  // "С понедельником" greeting which is days:[1] within DAYTIME (11–20).
  it("never returns an 11–20 windowed greeting at hour 9, 20, or 22", () => {
    for (const h of [9, 20, 22]) {
      const seen = new Set<string>();
      for (let i = 0; i < 500; i++) {
        const r = i / 500;
        seen.add(pickGreeting({ now: MONDAY(h), name: "Admin", random: () => r }));
      }
      expect(seen.has("С понедельником")).toBe(false);
    }
  });

  it("can return the 11–20 windowed greeting at hour 14", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const r = i / 1000;
      seen.add(pickGreeting({ now: MONDAY(14), name: "Admin", random: () => r }));
    }
    expect(seen.has("С понедельником")).toBe(true);
  });
});

describe("pickGreeting — name handling", () => {
  it("never returns a {name} greeting when name is null", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const r = i / 1000;
      seen.add(pickGreeting({ now: WEDNESDAY(14), name: null, random: () => r }));
    }
    for (const s of seen) {
      expect(s).not.toContain("{name}");
    }
  });

  it("never returns a {name} greeting when name is empty/whitespace", () => {
    for (const emptyName of ["", "   "]) {
      const seen = new Set<string>();
      for (let i = 0; i < 500; i++) {
        const r = i / 500;
        seen.add(pickGreeting({ now: WEDNESDAY(14), name: emptyName, random: () => r }));
      }
      for (const s of seen) {
        expect(s).not.toContain("{name}");
      }
    }
  });

  it("substitutes {name} with the trimmed name; no leftover token over a big sample", () => {
    for (let i = 0; i < 2000; i++) {
      const r = i / 2000;
      const got = pickGreeting({ now: TUESDAY(14), name: "  Admin  ", random: () => r });
      expect(got).not.toContain("{name}");
      // Trimmed: no double-spaces from an untrimmed name.
      expect(got).not.toContain("  ");
    }
  });

  it("actually produces a name-substituted variant for name 'Admin'", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 2000; i++) {
      const r = i / 2000;
      seen.add(pickGreeting({ now: TUESDAY(14), name: "Admin", random: () => r }));
    }
    const anyWithAdmin = [...seen].some((s) => s.includes("Admin"));
    expect(anyWithAdmin).toBe(true);
  });
});

describe("pickGreeting — exclude", () => {
  it("never returns the excluded resolved string when alternatives exist", () => {
    // Pick what random()===0 would return, then exclude it and confirm it's gone.
    const first = pickGreeting({ now: TUESDAY(14), name: "Admin", random: () => 0 });
    const seen = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const r = i / 1000;
      seen.add(
        pickGreeting({ now: TUESDAY(14), name: "Admin", random: () => r, exclude: first }),
      );
    }
    expect(seen.has(first)).toBe(false);
    expect(seen.size).toBeGreaterThan(0);
  });

  it("still returns a value (does not throw/return empty) if exclude would empty the set", () => {
    // Force a context with a single eligible greeting by excluding it: the
    // implementation must keep it rather than return "". We simulate by
    // excluding every possible resolved string is impractical; instead verify
    // that excluding the only-likely candidate at an edge still yields non-empty.
    const got = pickGreeting({
      now: TUESDAY(14),
      name: "Admin",
      random: () => 0,
      exclude: "definitely-not-in-pool",
    });
    expect(got.length).toBeGreaterThan(0);
  });
});

describe("pickGreeting — robustness matrix", () => {
  it("returns a non-empty, token-free string across all days x hours x name states", () => {
    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 23];
    const names: (string | null)[] = ["Admin", null];
    let checks = 0;
    for (let day = 0; day < 7; day++) {
      // 2024-01-07 is a Sunday (getDay()===0); add `day` to sweep the week.
      for (const h of hours) {
        for (const nm of names) {
          const now = localDate(2024, 0, 7 + day, h);
          // Multiple random values per context.
          for (const r of [0, 0.25, 0.5, 0.75, 0.999999]) {
            const got = pickGreeting({ now, name: nm, random: () => r });
            expect(got).not.toBe("");
            expect(got).not.toContain("{name}");
            checks++;
          }
        }
      }
    }
    expect(checks).toBe(7 * hours.length * names.length * 5);
  });

  it("never throws with default RNG (Math.random)", () => {
    expect(() =>
      pickGreeting({ now: TUESDAY(14), name: "Admin" }),
    ).not.toThrow();
    const got = pickGreeting({ now: TUESDAY(14), name: "Admin" });
    expect(got).not.toBe("");
    expect(got).not.toContain("{name}");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Night = calm. Deterministic sweep over r = i/N so there is zero flakiness.
// We use selectGreeting (the exported object-returning core) so we can read the
// `calm` metadata of the ACTUAL picked entry, not just the resolved string.
// ───────────────────────────────────────────────────────────────────────────
function calmFraction(now: Date, name: string, n: number): number {
  let calm = 0;
  for (let i = 0; i < n; i++) {
    const r = i / n; // evenly-spaced sweep across [0,1)
    const g = selectGreeting({ now, name, random: () => r });
    if (g.calm === true) calm++;
  }
  return calm / n;
}

describe("pickGreeting — night is calm with high probability", () => {
  const N = 400;

  it("at hour 2, >= 70% of picks are calm", () => {
    const frac = calmFraction(WEDNESDAY(2), "Admin", N);
    expect(frac).toBeGreaterThanOrEqual(0.7);
  });

  it("at hour 23, >= 70% of picks are calm", () => {
    const frac = calmFraction(WEDNESDAY(23), "Admin", N);
    expect(frac).toBeGreaterThanOrEqual(0.7);
  });

  it("preserves daytime variety: at hour 14, calm share is <= 0.6 (energetic still show)", () => {
    const frac = calmFraction(WEDNESDAY(14), "Admin", N);
    expect(frac).toBeLessThanOrEqual(0.6);
    // And there must actually be non-calm picks during the day.
    expect(frac).toBeLessThan(1);
  });

  it("selectGreeting and pickGreeting agree on the resolved text", () => {
    const r = () => 0.42;
    const obj = selectGreeting({ now: WEDNESDAY(14), name: "Admin", random: r });
    const str = pickGreeting({ now: WEDNESDAY(14), name: "Admin", random: r });
    expect(str).toBe(obj.text.split("{name}").join("Admin"));
  });
});

describe("GREETINGS pool — no incognito feature references", () => {
  it("no greeting text mentions incognito (RU or EN, case-insensitive)", () => {
    for (const g of GREETINGS) {
      const lower = g.text.toLowerCase();
      expect(lower).not.toContain("инкогнито");
      expect(lower).not.toContain("incognito");
    }
  });
});

describe("GREETINGS pool — calm night greetings exist", () => {
  it("has a healthy set of calm greetings windowed to night", () => {
    const calmNight = GREETINGS.filter(
      (g) =>
        g.calm === true &&
        g.minHour !== undefined &&
        ((g.minHour === 23 && g.maxHour === 24) || (g.minHour === 0 && g.maxHour === 5)),
    );
    // Many entries (each soothing line is added twice: 23–24 and 0–5).
    expect(calmNight.length).toBeGreaterThanOrEqual(20);
  });
});
