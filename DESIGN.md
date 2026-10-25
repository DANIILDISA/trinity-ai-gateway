# Trinity Design System

## Brand
Trinity is a ruble-denominated AI gateway for the Russian market.
Aesthetic direction: **dark, minimal, precision** — like a professional trading terminal meets modern AI product.
NOT: generic SaaS, purple gradients, rounded-everything, playful.

## Color Palette
All colors use the `neutral-*` scale. Do NOT use `zinc-*`, `gray-*`, or `slate-*` — only `neutral-*`.

### Backgrounds (darkest to lightest)
- Page/canvas: `bg-neutral-950` (#0a0a0a)
- Panel/card: `bg-neutral-900` (#171717)
- Elevated card: `bg-neutral-800` (#262626)
- Hover/active: `bg-neutral-700` (#404040)
- Subtle border: `border-neutral-800`
- Strong border: `border-neutral-700`

### Text
- Primary: `text-neutral-100`
- Secondary: `text-neutral-400`
- Muted: `text-neutral-500`
- Disabled: `text-neutral-600`

### Accent (brand cyan)
- Primary accent: `#22d3ee` (cyan-400) — for highlights, active states, progress
- Secondary accent: `#0ea5e9` (sky-500) — for links, secondary CTAs

### Tier Colors (DO NOT CHANGE — these are product-critical)
- Free/Standard: `#94a3b8` (slate-400 — intentionally separate from neutral scale)
- Pro: `#34d399` (emerald-400) — carries the Trinity brand mark (NeuralIcon) + brand accent
- Ultra: `#f472b6` (pink-400) or gold `#fbbf24` — see ModelTierIcon.tsx
- Reasoning badge: `#a78bfa` (violet-400)

### Semantic
- Success: `text-emerald-400`
- Error: `text-red-400`
- Warning: `text-amber-400`
- Cost/price: `text-emerald-400`

## Typography
- Font: Geist (already in project via next/font)
- **Assistant answer bodies use an elegant serif** (`font-serif` → Source Serif 4,
  Cyrillic, loaded via next/font in the root layout) to match the Claude reading
  aesthetic the product owner requested. This is a deliberate exception to the
  sans-only rule and applies ONLY to the rendered markdown of assistant messages
  (`Markdown.tsx`). UI chrome, thinking text, user messages, and code stay sans/mono.
- Monospace (tokens, prices, stats): `font-mono text-xs`
- Headers in panels: `text-xs font-semibold uppercase tracking-wider text-neutral-500`
- Body: `text-sm text-neutral-300`

## Spacing & Layout
- Panel border-radius: `rounded-lg` (8px) — not rounded-xl, not rounded-2xl
- Small elements (badges, chips): `rounded-md` (6px)
- Standard padding inside panels: `p-4`
- Gap between panels: `gap-3`
- Chat height: `h-[calc(100vh-6.5rem)]`

## Component Patterns

### Message Bubbles
- User: `bg-neutral-800 text-neutral-100 rounded-2xl rounded-br-sm`
- Assistant: `bg-neutral-900 border border-neutral-800 text-neutral-200 rounded-2xl rounded-bl-sm`
- NO bg-white anywhere in chat

### Panels / Sidebars
- Background: `bg-neutral-900`
- Border: `border-r border-neutral-800` (for sidebars)
- Header rows: `border-b border-neutral-800`

### Buttons
- Primary: `bg-neutral-100 text-neutral-900 hover:bg-white` (light button on dark bg)
- Secondary: `bg-neutral-800 text-neutral-200 hover:bg-neutral-700 border border-neutral-700`
- Ghost: `text-neutral-400 hover:text-neutral-100 hover:bg-neutral-800`

### Inputs
- `bg-neutral-900 border border-neutral-700 text-neutral-100 placeholder:text-neutral-500`
- Focus: `focus:border-neutral-500 focus:ring-0`

## Animations (from tailwind.config.ts)
Available keyframes: `shimmer`, `fadeIn`, `scaleIn`
- New messages: `animate-fadeIn`
- Modal open: `animate-scaleIn`
- Loading states: `animate-shimmer`
- Micro-interactions: prefer `transition-colors duration-150` and `transition-all duration-200`

## Strict Rules
1. NEVER use `bg-white` or `bg-gray-*` inside `(main)` routes
2. NEVER use `zinc-*` — unify everything to `neutral-*`
3. Admin routes (`/admin/*`) are EXCLUDED from this design system — do not touch them
4. Auth routes (`/auth/*`) use their own layout — minimal changes only
5. `tsc --noEmit` must pass after every change
6. Functional parity must be maintained — no feature removals

## What Makes Trinity Distinct
- Animated tier icons (ModelTierIcon.tsx) — preserve and expand this pattern
- MicroAnalytics panel — dark, data-dense, terminal-like
- ModelSelector modal — already dark, the reference for other components
- Pricing shown per-model in rubles — always `font-mono`