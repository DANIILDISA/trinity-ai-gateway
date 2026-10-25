// Dynamic greeting engine for Trinity.
//
// The pool is BILINGUAL: mostly Russian greetings with a sprinkle of catchy
// English ones. Each greeting is a template that may contain the literal token
// "{name}"; pickGreeting resolves it against the user's display name and applies
// day/hour eligibility filtering plus weighted random selection.
//
// Public contract (relied on by other modules — do not deviate):
//   - GREETINGS: the pool (>= 100 entries)
//   - pickGreeting(opts): returns a ready-to-display string, never "" and never throws

export type Greeting = {
  text: string; // template; may contain the literal token "{name}"
  days?: number[]; // allowed days, 0=Sunday .. 6=Saturday; omitted = any day
  minHour?: number; // inclusive lower bound on local hour 0-23; omitted = no lower bound
  maxHour?: number; // EXCLUSIVE upper bound on local hour 0-24; omitted = no upper bound
  weight?: number; // relative selection weight when eligible (default 1)
  calm?: boolean; // metadata: marks soothing/neutral greetings. The pick logic
  // does NOT read this — it exists for weighting/organization/tests so we can
  // measure the "calm share" at night vs day.
};

export interface GreetingPickOptions {
  now: Date; // current time (injected for testability)
  name?: string | null; // user's display name, may be empty/null
  random?: () => number; // injectable RNG returning a float in [0,1); defaults to Math.random
  exclude?: string | null; // a previously-shown RESOLVED string to avoid repeating
}

// Convenience hour windows so the pool reads clearly.
const MORNING = { minHour: 5, maxHour: 12 } as const;
const AFTERNOON = { minHour: 12, maxHour: 17 } as const;
const EVENING = { minHour: 17, maxHour: 23 } as const;
const NIGHT_LATE = { minHour: 23, maxHour: 24 } as const; // 23:00–23:59
const NIGHT_EARLY = { minHour: 0, maxHour: 5 } as const; // 00:00–04:59
// Daytime window used for day-of-week specials so they only show during the day.
const DAYTIME = { minHour: 11, maxHour: 20 } as const;
// Broad waking-hours window for energetic/upbeat UNIVERSAL greetings. These are
// loud ("Поехали", "Готов творить") and would dominate the night otherwise, so
// we constrain them to 07:00–22:59. During deep night (23–07) they drop out,
// leaving the calm pool to dominate.
const WAKING = { minHour: 7, maxHour: 23 } as const;

const SPECIAL_WEIGHT = 3;
// Calm night greetings are deliberately heavy so that, once the energetic
// universals are gone (07–23 window), the calm share at night is well above 70%.
const CALM_NIGHT_WEIGHT = 6;

export const GREETINGS: Greeting[] = [
  // ───────────────────────────────────────────────────────────────────────────
  // TIME-OF-DAY (Russian primary), with and without {name}.
  // ───────────────────────────────────────────────────────────────────────────
  // Morning 5–12
  { text: "Доброе утро", ...MORNING },
  { text: "Доброе утро, {name}", ...MORNING },
  { text: "Утро доброе!", ...MORNING },
  { text: "С добрым утром, {name}", ...MORNING },
  { text: "Доброе утречко", ...MORNING },
  // Afternoon 12–17
  { text: "Добрый день", ...AFTERNOON },
  { text: "Добрый день, {name}", ...AFTERNOON },
  { text: "Доброго дня", ...AFTERNOON },
  { text: "Хорошего дня, {name}", ...AFTERNOON },
  // Evening 17–23
  { text: "Добрый вечер", ...EVENING, calm: true },
  { text: "Добрый вечер, {name}", ...EVENING, calm: true },
  { text: "Доброго вечера", ...EVENING, calm: true },
  { text: "Тихого вечера, {name}", ...EVENING, calm: true },
  // Night — two entries covering 23–24 and 0–5. All calm/soothing.
  { text: "Доброй ночи", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Доброй ночи, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Доброй ночи", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Доброй ночи, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Спокойной ночи", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Полуночничаем?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },

  // ───────────────────────────────────────────────────────────────────────────
  // MORNING coffee
  // ───────────────────────────────────────────────────────────────────────────
  { text: "Кофе и Trinity?", ...MORNING, weight: 2 },
  { text: "Coffee and Trinity time?", ...MORNING, weight: 2 },
  { text: "Кофе уже налит, {name}?", ...MORNING, weight: 2 },
  { text: "Первый кофе, первый запрос?", ...MORNING },

  // ───────────────────────────────────────────────────────────────────────────
  // DAY-OF-WEEK specials (mix RU + EN), daytime window, higher weight.
  // 0=Sunday .. 6=Saturday
  // ───────────────────────────────────────────────────────────────────────────
  // Monday (1)
  { text: "С понедельником", days: [1], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "С понедельником, {name}", days: [1], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Happy Monday, {name}", days: [1], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Понедельник — начало больших дел", days: [1], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Tuesday (2)
  { text: "Хорошего вторника", days: [2], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Хорошего вторника, {name}", days: [2], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Happy Tuesday", days: [2], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Wednesday (3)
  { text: "Середина недели — держимся", days: [3], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Хорошей среды, {name}", days: [3], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Happy hump day", days: [3], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Thursday (4)
  { text: "Хорошего четверга", days: [4], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Почти пятница, {name}", days: [4], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Happy Thursday", days: [4], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Friday (5)
  { text: "С пятницей!", days: [5], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Та самая пятничная атмосфера", days: [5], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "That Friday feeling, {name}", days: [5], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Happy Friday", days: [5], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Пятница — это маленькая жизнь, {name}", days: [5], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Saturday (6) — weekend
  { text: "Хороших выходных", days: [6], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Хороших выходных, {name}", days: [6], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Welcome to the weekend", days: [6], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Выходные, наконец-то", days: [6], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Суббота в радость, {name}", days: [6], ...DAYTIME, weight: SPECIAL_WEIGHT },
  // Sunday (0) — weekend
  { text: "Хороших выходных", days: [0], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Спокойного воскресенья", days: [0], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Welcome to the weekend", days: [0], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Sunday session, {name}?", days: [0], ...DAYTIME, weight: SPECIAL_WEIGHT },
  { text: "Воскресный настрой, {name}", days: [0], ...DAYTIME, weight: SPECIAL_WEIGHT },

  // ───────────────────────────────────────────────────────────────────────────
  // NIGHT-OWL specials (night windows), higher weight.
  // ───────────────────────────────────────────────────────────────────────────
  { text: "Привет, ночная сова", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Привет, ночная сова, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Hello, night owl", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "О чём думаешь этой ночью?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "What's on your mind tonight?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Не спится, {name}?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Привет, ночная сова", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Hello, night owl", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  // Slightly more productive in tone — kept non-calm and at the regular special
  // weight so they remain a small minority of night picks.
  { text: "Ночь — лучшее время для идей", ...NIGHT_LATE, weight: SPECIAL_WEIGHT },
  { text: "Working late, {name}?", ...NIGHT_LATE, weight: SPECIAL_WEIGHT },

  // ───────────────────────────────────────────────────────────────────────────
  // GENERIC friendly — universal (no conditions). RU primary + a few EN,
  // with and without {name}. These guarantee eligibility is never empty.
  // ───────────────────────────────────────────────────────────────────────────
  // — Truly NEUTRAL universals: eligible at ANY hour (incl. deep night) and
  //   marked calm so they count toward the calm share at night. —
  { text: "Привет", calm: true },
  { text: "Привет, {name}", calm: true },
  { text: "Рад видеть", calm: true },
  { text: "Рад видеть, {name}", calm: true },
  { text: "С возвращением", calm: true },
  { text: "С возвращением, {name}", calm: true },
  { text: "О чём думаешь?", calm: true },
  { text: "О чём думаешь, {name}?", calm: true },
  { text: "Как дела?", calm: true },
  { text: "Как дела, {name}?", calm: true },
  { text: "Как настроение?", calm: true },
  { text: "Как настроение, {name}?", calm: true },
  { text: "Давно не виделись", calm: true },
  { text: "Давно не виделись, {name}", calm: true },
  { text: "Приветствую, кем бы ты ни был", calm: true },
  { text: "Чем могу помочь?", calm: true },
  { text: "Чем могу помочь, {name}?", calm: true },
  { text: "Рад снова видеть тебя", calm: true },
  { text: "Рад снова видеть тебя, {name}", calm: true },
  { text: "Привет-привет", calm: true },
  { text: "На связи", calm: true },
  { text: "На связи, {name}", calm: true },
  { text: "Я весь внимание", calm: true },
  { text: "Я весь внимание, {name}", calm: true },
  { text: "Рад, что ты здесь", calm: true },
  { text: "Рад, что ты здесь, {name}", calm: true },
  { text: "Готов слушать", calm: true },
  { text: "Готов слушать, {name}", calm: true },
  { text: "Слушаю внимательно", calm: true },
  { text: "Открыт к диалогу", calm: true },
  { text: "Открыт к диалогу, {name}", calm: true },
  { text: "Всегда на связи", calm: true },
  { text: "Рад знакомству", calm: true },
  { text: "Поговорим?", calm: true },
  { text: "Поговорим, {name}?", calm: true },

  // — ENERGETIC / upbeat universals: constrained to waking hours 07–23 so they
  //   do NOT crowd out the calm pool during deep night. Not calm. —
  { text: "Что нового?", ...WAKING },
  { text: "Что нового, {name}?", ...WAKING },
  { text: "Чем займёмся?", ...WAKING },
  { text: "Чем займёмся, {name}?", ...WAKING },
  { text: "Готов творить?", ...WAKING },
  { text: "Готова творить?", ...WAKING },
  { text: "Готов(а) творить, {name}?", ...WAKING },
  { text: "Поехали", ...WAKING },
  { text: "Поехали, {name}", ...WAKING },
  { text: "Снова в деле", ...WAKING },
  { text: "Снова в деле, {name}", ...WAKING },
  { text: "Как прошёл день?", ...WAKING },
  { text: "Как прошёл день, {name}?", ...WAKING },
  { text: "С чего начнём?", ...WAKING },
  { text: "С чего начнём, {name}?", ...WAKING },
  { text: "Какие планы?", ...WAKING },
  { text: "Какие планы, {name}?", ...WAKING },
  { text: "Что задумал?", ...WAKING },
  { text: "Что задумал, {name}?", ...WAKING },
  { text: "Чем сегодня удивишь?", ...WAKING },
  { text: "За дело?", ...WAKING },
  { text: "За дело, {name}?", ...WAKING },
  { text: "Спрашивай — отвечу", ...WAKING },
  { text: "Начнём с чистого листа?", ...WAKING },
  { text: "Какой вопрос на этот раз?", ...WAKING },
  { text: "Какой вопрос на этот раз, {name}?", ...WAKING },
  { text: "Время для новой идеи", ...WAKING },
  { text: "Время для новой идеи, {name}", ...WAKING },
  { text: "Давай поразмышляем", ...WAKING },
  { text: "Давай поразмышляем, {name}", ...WAKING },
  { text: "Чем сегодня займёмся?", ...WAKING },
  { text: "Привет, как настрой?", ...WAKING },
  { text: "Привет, как настрой, {name}?", ...WAKING },
  { text: "Снова вместе", ...WAKING },
  { text: "Снова вместе, {name}", ...WAKING },
  { text: "Что обсудим сегодня?", ...WAKING },
  { text: "Что обсудим сегодня, {name}?", ...WAKING },
  { text: "Готов помочь с чем угодно", ...WAKING },
  { text: "Готов помочь с чем угодно, {name}", ...WAKING },
  { text: "Идеи приветствуются", ...WAKING },
  { text: "Бросай вопрос", ...WAKING },
  { text: "Бросай вопрос, {name}", ...WAKING },
  { text: "Какая задача на сегодня?", ...WAKING },
  { text: "Какая задача на сегодня, {name}?", ...WAKING },
  { text: "Чем порадуешь?", ...WAKING },
  { text: "Чем порадуешь, {name}?", ...WAKING },
  { text: "Заглянул в гости?", ...WAKING },
  { text: "Заглянул в гости, {name}?", ...WAKING },
  { text: "Что нового в твоём мире?", ...WAKING },
  { text: "Что нового в твоём мире, {name}?", ...WAKING },
  // A few catchy English universals. Calm/neutral ones stay universal; the
  // upbeat "let's go" ones are constrained to waking hours like their RU peers.
  { text: "Hey there", calm: true },
  { text: "Hey there, {name}", calm: true },
  { text: "Welcome", calm: true },
  { text: "Welcome, {name}", calm: true },
  { text: "Welcome back", calm: true },
  { text: "Welcome back, {name}", calm: true },
  { text: "How can I help?", calm: true },
  { text: "How can I help, {name}?", calm: true },
  { text: "What's on your mind?", calm: true },
  { text: "What's on your mind, {name}?", calm: true },
  { text: "Good to see you", calm: true },
  { text: "Good to see you, {name}", calm: true },
  { text: "What's new?", ...WAKING },
  { text: "What's new, {name}?", ...WAKING },
  { text: "Ready when you are", ...WAKING },
  { text: "Ready when you are, {name}", ...WAKING },
  { text: "Let's get started", ...WAKING },
  { text: "Let's get started, {name}", ...WAKING },

  // ───────────────────────────────────────────────────────────────────────────
  // NEW calm/soothing NIGHT greetings (Russian primary + a few English).
  // Quiet, warm, restful, unhurried. Each spans the night via two windowed
  // entries (23–24 and 0–5). Elevated weight so the night-calm share is high.
  // ───────────────────────────────────────────────────────────────────────────
  { text: "Тихой ночи", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Тихой ночи", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Тихой ночи, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Тихой ночи, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Поздний час — можно выдохнуть", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Поздний час — можно выдохнуть", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Ночь — время для спокойных мыслей", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Ночь — время для спокойных мыслей", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Здесь тихо и спокойно", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Здесь тихо и спокойно", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Время замедлиться", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Время замедлиться", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Пусть будет спокойно", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Пусть будет спокойно", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Тихо. Не торопись.", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Тихо. Не торопись.", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Уютной ночи, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Уютной ночи, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Спокойной ночи, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Спокойной ночи, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Пусть ночь будет тихой", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Пусть ночь будет тихой", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Не спится?", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Не спится?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Отдыхаешь?", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Отдыхаешь?", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Можно просто помолчать", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Можно просто помолчать", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Спокойствие — тоже работа, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Спокойствие — тоже работа, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  // A few calm English night ones.
  { text: "Quiet hours", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Quiet hours", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Rest easy, {name}", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Rest easy, {name}", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Take it slow tonight", ...NIGHT_LATE, calm: true, weight: CALM_NIGHT_WEIGHT },
  { text: "Take it slow tonight", ...NIGHT_EARLY, calm: true, weight: CALM_NIGHT_WEIGHT },
];

/**
 * Resolve a template by substituting every "{name}" token with the trimmed name.
 */
function resolve(text: string, trimmedName: string): string {
  return text.split("{name}").join(trimmedName);
}

// Sentinel returned by selectGreeting when the eligible set is somehow empty.
// The pool guarantees universal no-condition no-{name} greetings, so in practice
// this is unreachable — it exists to satisfy the never-"" contract.
const FALLBACK_GREETING: Greeting = { text: "Привет", calm: true };

/**
 * Core selection: returns the chosen Greeting OBJECT (template not yet resolved)
 * for the given options. Exposed primarily so tests can inspect metadata (e.g.
 * `calm`) of the actually-picked entry rather than only the resolved string.
 *
 * pickGreeting is a thin wrapper that resolves the {name} token on the result,
 * so its public signature is unchanged.
 */
export function selectGreeting(opts: GreetingPickOptions): Greeting {
  const { now, name, exclude } = opts;
  const random = opts.random ?? Math.random;

  const h = now.getHours();
  const d = now.getDay();
  const trimmedName = name ? name.trim() : "";
  const hasName = trimmedName.length > 0;

  // Step 2: eligibility filter.
  const eligible = GREETINGS.filter((g) => {
    if (g.days && !g.days.includes(d)) return false;
    if (g.minHour !== undefined && h < g.minHour) return false;
    if (g.maxHour !== undefined && h >= g.maxHour) return false;
    if (g.text.includes("{name}") && !hasName) return false;
    return true;
  });

  if (eligible.length === 0) {
    return FALLBACK_GREETING;
  }

  // Step 4: drop any whose resolved form equals `exclude`, unless that empties
  // the set (then keep them).
  let candidates = eligible;
  if (exclude != null) {
    const filtered = eligible.filter((g) => resolve(g.text, trimmedName) !== exclude);
    if (filtered.length > 0) {
      candidates = filtered;
    }
  }

  // Step 5: weighted random pick. Build cumulative weights in array order;
  // r = random()*totalWeight; return the first whose cumulative weight > r.
  let totalWeight = 0;
  for (const g of candidates) {
    totalWeight += g.weight ?? 1;
  }

  const r = random() * totalWeight;
  let cumulative = 0;
  let picked = candidates[candidates.length - 1];
  for (const g of candidates) {
    cumulative += g.weight ?? 1;
    if (cumulative > r) {
      picked = g;
      break;
    }
  }

  return picked;
}

/**
 * Returns a ready-to-display greeting string with {name} substituted (or a
 * no-name variant chosen). Never throws and never returns "".
 */
export function pickGreeting(opts: GreetingPickOptions): string {
  const trimmedName = opts.name ? opts.name.trim() : "";
  const picked = selectGreeting(opts);
  return resolve(picked.text, trimmedName);
}
