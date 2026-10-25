"use client";

import { useState, type CSSProperties, type ReactElement } from "react";

export type TierKey =
  | "nano"
  | "micro"
  | "light"
  | "standard"
  | "pro"
  | "ultra"
  | "max";

export interface TierInfo {
  tier: number;
  name: string;
  color: string;
  icon: TierKey;
}

// Tier thresholds are in ₽ per 1M input tokens. Boundaries are inclusive on the
// lower end (e.g. exactly 5 ₽/M is "Micro").
export function getTierInfo(price: number): TierInfo {
  const p = Number.isFinite(price) && price > 0 ? price : 0;
  if (p < 5) return { tier: 1, name: "Nano", color: "#94a3b8", icon: "nano" };
  if (p < 20) return { tier: 2, name: "Micro", color: "#22d3ee", icon: "micro" };
  if (p < 80) return { tier: 3, name: "Light", color: "#60a5fa", icon: "light" };
  if (p < 300)
    return { tier: 4, name: "Standard", color: "#a78bfa", icon: "standard" };
  // The Pro tier carries the Trinity brand mark (NeuralIcon) and its emerald
  // accent (#34d399) — see the "pro" glyph below.
  if (p < 1000)
    return { tier: 5, name: "Pro", color: "#34d399", icon: "pro" };
  if (p < 3000)
    return { tier: 6, name: "Ultra", color: "#fb923c", icon: "ultra" };
  return { tier: 7, name: "Max", color: "#fbbf24", icon: "max" };
}

// Peak glow radius (px) per tier — the rest state is always 4px. Heavier tiers
// glow harder so the hierarchy reads even before the animation registers.
const GLOW_INTENSITY: Record<TierKey, number> = {
  nano: 8,
  micro: 12,
  light: 14,
  standard: 16,
  pro: 22,
  ultra: 22,
  max: 20,
};

// All per-element keyframes live here so they ship with the component (Tailwind
// can't register arbitrary keyframes without a config rebuild). Duplicate
// <style> tags across icon instances are harmless — identical rules dedupe.
// Every animation is transform/opacity/stroke-dashoffset only: GPU compositing
// (transform/opacity) or cheap paint (dashoffset) — never layout.
const KEYFRAMES = `
@keyframes mti_breathe { 0%,100% { transform: scale(1); } 50% { transform: scale(1.18); } }
@keyframes mti_breathe_soft { 0%,100% { transform: scale(1); } 50% { transform: scale(1.07); } }
@keyframes mti_pulse { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
@keyframes mti_pulse_mid { 0%,100% { opacity: 0.12; } 50% { opacity: 0.7; } }
@keyframes mti_node { 0%,100% { transform: scale(1); opacity: 0.5; } 50% { transform: scale(1.4); opacity: 1; } }
@keyframes mti_neural_node { 0%,100% { opacity: 0.35; transform: scale(1); } 50% { opacity: 1; transform: scale(1.25); } }
@keyframes mti_neural_edge { 0%,100% { opacity: 0.12; } 50% { opacity: 0.5; } }
@keyframes mti_spin { to { transform: rotate(360deg); } }
@keyframes mti_spin_rev { to { transform: rotate(-360deg); } }
@keyframes mti_flow { to { stroke-dashoffset: -13; } }
@keyframes mti_flow_rev { to { stroke-dashoffset: 13; } }
@keyframes mti_core { 0%,100% { transform: scale(1); } 50% { transform: scale(1.5); } }
@keyframes mti_halo { 0%,100% { transform: scale(1); opacity: 0.45; } 50% { transform: scale(0.62); opacity: 0.95; } }
@keyframes mti_shock { 0% { transform: scale(0.55); opacity: 0.7; } 70% { opacity: 0; } 100% { transform: scale(1.45); opacity: 0; } }
@keyframes mti_ray { 0%,100% { stroke-dashoffset: 7; opacity: 0.25; } 50% { stroke-dashoffset: 0; opacity: 1; } }
@keyframes mti_rise { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-1.8px); } }
@keyframes mti_orbit5 {
  0% { transform: translate(5px,0); } 12.5% { transform: translate(3.54px,3.54px); }
  25% { transform: translate(0,5px); } 37.5% { transform: translate(-3.54px,3.54px); }
  50% { transform: translate(-5px,0); } 62.5% { transform: translate(-3.54px,-3.54px); }
  75% { transform: translate(0,-5px); } 87.5% { transform: translate(3.54px,-3.54px); }
  100% { transform: translate(5px,0); }
}
@keyframes mti_orbit8 {
  0% { transform: translate(8px,0); } 12.5% { transform: translate(5.66px,5.66px); }
  25% { transform: translate(0,8px); } 37.5% { transform: translate(-5.66px,5.66px); }
  50% { transform: translate(-8px,0); } 62.5% { transform: translate(-5.66px,-5.66px); }
  75% { transform: translate(0,-8px); } 87.5% { transform: translate(5.66px,-5.66px); }
  100% { transform: translate(8px,0); }
}
`;

interface AnimOpts {
  timing?: string;
  delay?: string;
  // SVG-userspace transform origin (default = viewBox center, 12 12).
  origin?: string;
}

// Build the inline animation style for an element, but only while active.
// transform-box: view-box makes transforms resolve in viewBox units, so
// translate px == user units and rotation/scale pivot around the named point.
function animStyle(
  active: boolean,
  name: string,
  duration: string,
  opts: AnimOpts = {},
): CSSProperties {
  if (!active) return {};
  const { timing = "linear", delay = "0s", origin = "center" } = opts;
  return {
    animation: `${name} ${duration} ${timing} ${delay} infinite`,
    transformBox: "view-box",
    transformOrigin: origin,
  };
}

type Pt = [number, number];

// Point on a circle centred at the 24×24 viewBox centre (12,12).
const ptOnCircle = (r: number, deg: number): Pt => {
  const a = (deg * Math.PI) / 180;
  return [12 + r * Math.cos(a), 12 + r * Math.sin(a)];
};

const r2 = (n: number): number => Math.round(n * 100) / 100;

function renderGlyph(
  icon: TierKey,
  color: string,
  active: boolean,
): ReactElement {
  const fill = { fill: color, stroke: "none" } as const;

  switch (icon) {
    case "nano": {
      // Humble single-electron atom — one faint outer shell, a slowly rotating
      // dashed inner shell, a soft breathing nucleus and a lone electron that
      // traces the outer shell. Deliberately sparse and quiet (cheapest tier).
      return (
        <>
          <circle cx="12" cy="12" r="8" strokeWidth={0.6} opacity={0.22} />
          <circle
            cx="12"
            cy="12"
            r="4.6"
            strokeWidth={0.6}
            opacity={0.4}
            strokeDasharray="1.2 3.4"
            style={animStyle(active, "mti_spin", "16s", { origin: "12px 12px" })}
          />
          <circle cx="12" cy="12" r="2.7" strokeWidth={0.48} opacity={0.3} />
          <circle
            cx="12"
            cy="12"
            r="1.9"
            {...fill}
            opacity={0.95}
            style={animStyle(active, "mti_breathe", "3.2s", {
              timing: "ease-in-out",
              origin: "12px 12px",
            })}
          />
          <circle
            cx="12"
            cy="12"
            r="1"
            {...fill}
            style={animStyle(active, "mti_orbit8", "6s")}
          />
        </>
      );
    }

    case "micro": {
      // DNA double helix — two offset S-curve strands crossing at the centre,
      // with ladder rungs that carry nucleotide caps. The whole helix floats
      // gently while a bright pulse climbs the rungs bottom→top (replication).
      const rungs = [4, 7, 12, 17, 20];
      return (
        <g style={animStyle(active, "mti_rise", "3s", { timing: "ease-in-out" })}>
          <path
            d="M 9 2 C 6 5, 6 8, 9 10 C 12 12, 12 14, 9 16 C 6 18, 6 21, 9 22"
            strokeWidth={1.2}
          />
          <path
            d="M 15 2 C 18 5, 18 8, 15 10 C 12 12, 12 14, 15 16 C 18 18, 18 21, 15 22"
            strokeWidth={1.2}
          />
          {rungs.map((y, i) => {
            const delay = `${(rungs.length - 1 - i) * 0.2}s`;
            return (
              <g
                key={y}
                style={animStyle(active, "mti_pulse", "1.6s", {
                  timing: "ease-in-out",
                  delay,
                })}
                opacity={0.55}
              >
                <line x1="9" y1={y} x2="15" y2={y} strokeWidth={0.8} />
                <circle cx="9" cy={y} r="0.85" {...fill} />
                <circle cx="15" cy={y} r="0.85" {...fill} />
              </g>
            );
          })}
        </g>
      );
    }

    case "light": {
      // Layered neural lattice — a 2→3→2 fully-connected net. A signal sweeps
      // left→right: input edges fire first, hidden nodes throb, then output
      // edges fire and output nodes light. The Light-tier blue.
      const layers: { nodes: Pt[]; nodeDelay: string }[] = [
        { nodes: [[5, 9], [5, 15]], nodeDelay: "0s" },
        { nodes: [[12, 5], [12, 12], [12, 19]], nodeDelay: "0.25s" },
        { nodes: [[19, 9], [19, 15]], nodeDelay: "0.5s" },
      ];
      const [L, M, R] = layers;
      const edges: { a: Pt; b: Pt; delay: string }[] = [];
      for (const a of L.nodes) for (const b of M.nodes) edges.push({ a, b, delay: "0s" });
      for (const a of M.nodes) for (const b of R.nodes) edges.push({ a, b, delay: "0.45s" });
      return (
        <>
          {edges.map((e, i) => (
            <line
              key={`e${i}`}
              x1={e.a[0]}
              y1={e.a[1]}
              x2={e.b[0]}
              y2={e.b[1]}
              strokeWidth={0.72}
              opacity={0.22}
              style={animStyle(active, "mti_pulse_mid", "1.6s", {
                timing: "ease-in-out",
                delay: e.delay,
              })}
            />
          ))}
          {layers.flatMap((layer) =>
            layer.nodes.map((n, i) => (
              <circle
                key={`${n[0]}-${n[1]}-${i}`}
                cx={n[0]}
                cy={n[1]}
                r="1.5"
                {...fill}
                style={animStyle(active, "mti_node", "1.8s", {
                  timing: "ease-in-out",
                  delay: layer.nodeDelay,
                  origin: `${n[0]}px ${n[1]}px`,
                })}
              />
            )),
          )}
        </>
      );
    }

    case "standard": {
      // Cognition core (cerebral) — a slowly rotating hexagonal lattice frames
      // a pulsing mind-core, with six dendrites radiating outward. A bright
      // signal flows out along each dashed dendrite to a twinkling synapse,
      // staggered around the ring so thoughts appear to fire in sequence.
      const hex = Array.from({ length: 6 }, (_, k) => ptOnCircle(9.2, k * 60));
      const hexPts = hex.map((p) => `${r2(p[0])},${r2(p[1])}`).join(" ");
      const dendrites = Array.from({ length: 6 }, (_, k) => {
        const ang = k * 60 + 30;
        return { inner: ptOnCircle(3, ang), tip: ptOnCircle(8.4, ang), delay: `${k * 0.18}s` };
      });
      return (
        <>
          <polygon
            points={hexPts}
            strokeWidth={0.6}
            opacity={0.28}
            style={animStyle(active, "mti_spin", "18s", { origin: "12px 12px" })}
          />
          {dendrites.map((d, i) => (
            <line
              key={`d${i}`}
              x1={r2(d.inner[0])}
              y1={r2(d.inner[1])}
              x2={r2(d.tip[0])}
              y2={r2(d.tip[1])}
              strokeWidth={0.8}
              strokeDasharray="2 4.2"
              opacity={0.85}
              style={animStyle(active, "mti_flow", "1.4s", { delay: d.delay })}
            />
          ))}
          {dendrites.map((d, i) => (
            <circle
              key={`t${i}`}
              cx={r2(d.tip[0])}
              cy={r2(d.tip[1])}
              r="0.95"
              {...fill}
              style={animStyle(active, "mti_pulse", "1.6s", {
                timing: "ease-in-out",
                delay: d.delay,
              })}
            />
          ))}
          <circle
            cx="12"
            cy="12"
            r="3.6"
            strokeWidth={0.6}
            opacity={0.45}
            style={animStyle(active, "mti_halo", "2.2s", {
              timing: "ease-in-out",
              origin: "12px 12px",
            })}
          />
          <circle
            cx="12"
            cy="12"
            r="2.1"
            {...fill}
            style={animStyle(active, "mti_breathe", "2.4s", {
              timing: "ease-in-out",
              origin: "12px 12px",
            })}
          />
        </>
      );
    }

    case "pro": {
      // The Trinity brand mark, 1:1. This is the exact NeuralIcon geometry
      // (2→3→2 fully-connected nodes laid out in its 40×40 space) scaled ×0.6
      // into this 24×24 viewBox, carrying the brand emerald. On hover it
      // breathes with NeuralIcon's idle keyframes — nodes pulse on an i·0.18s
      // cascade, edges flicker on an (i%6)·0.18s cascade — so the Pro tier badge
      // and the header logo are visually one and the same.
      const S = 0.6;
      const sc = (p: Pt): Pt => [r2(p[0] * S), r2(p[1] * S)];
      const LEFT: Pt[] = [[8, 14], [8, 26]];
      const MID: Pt[] = [[20, 8], [20, 20], [20, 32]];
      const RIGHT: Pt[] = [[32, 14], [32, 26]];
      const nodes: Pt[] = [...LEFT, ...MID, ...RIGHT].map(sc);
      const edges: { a: Pt; b: Pt }[] = [];
      for (const l of LEFT) for (const m of MID) edges.push({ a: sc(l), b: sc(m) });
      for (const m of MID) for (const r of RIGHT) edges.push({ a: sc(m), b: sc(r) });
      return (
        <>
          {edges.map((e, i) => (
            <line
              key={`e${i}`}
              x1={e.a[0]}
              y1={e.a[1]}
              x2={e.b[0]}
              y2={e.b[1]}
              strokeWidth={0.58}
              opacity={0.4}
              style={animStyle(active, "mti_neural_edge", "3s", {
                timing: "ease-in-out",
                delay: `${(i % 6) * 0.18}s`,
              })}
            />
          ))}
          {nodes.map((n, i) => (
            <circle
              key={`n${i}`}
              cx={n[0]}
              cy={n[1]}
              r="1.44"
              {...fill}
              opacity={0.9}
              style={animStyle(active, "mti_neural_node", "2.4s", {
                timing: "ease-in-out",
                delay: `${i * 0.18}s`,
                origin: `${n[0]}px ${n[1]}px`,
              })}
            />
          ))}
        </>
      );
    }

    case "ultra": {
      // Exploding supernova — 3 expanding shockwave rings, 8 long diamond-tipped
      // rays + 8 short rays, two counter-rotating dashed energy rings, and an
      // 8-pointed star core that pulses fast. Rays extend one-by-one clockwise.
      const primary = Array.from({ length: 8 }, (_, i) => ({
        inner: ptOnCircle(3, i * 45),
        outer: ptOnCircle(10, i * 45),
      }));
      const secondary = Array.from({ length: 8 }, (_, i) => ({
        inner: ptOnCircle(3, 22.5 + i * 45),
        outer: ptOnCircle(7, 22.5 + i * 45),
      }));
      const diamond = (cx: number, cy: number): string =>
        `${cx},${r2(cy - 1.3)} ${r2(cx + 1.3)},${cy} ${cx},${r2(cy + 1.3)} ${r2(cx - 1.3)},${cy}`;
      const starPts = Array.from({ length: 16 }, (_, k) => {
        const [x, y] = ptOnCircle(k % 2 === 0 ? 3 : 1.3, k * 22.5);
        return `${r2(x)},${r2(y)}`;
      }).join(" ");
      const shockRings = [
        { r: 9, sw: 0.4, dur: "1.8s", delay: "0s" },
        { r: 7, sw: 0.64, dur: "2s", delay: "0.4s" },
        { r: 5, sw: 0.8, dur: "1.5s", delay: "0.8s" },
      ];
      return (
        <>
          {shockRings.map((rg, i) => (
            <circle
              key={`sw${i}`}
              cx="12"
              cy="12"
              r={rg.r}
              strokeWidth={rg.sw}
              style={animStyle(active, "mti_shock", rg.dur, {
                timing: "ease-out",
                delay: rg.delay,
                origin: "12px 12px",
              })}
            />
          ))}
          {primary.map((p, i) => (
            <line
              key={`p${i}`}
              x1={r2(p.inner[0])}
              y1={r2(p.inner[1])}
              x2={r2(p.outer[0])}
              y2={r2(p.outer[1])}
              strokeWidth={1.2}
              strokeDasharray="7"
              style={animStyle(active, "mti_ray", "1.2s", {
                timing: "ease-in-out",
                delay: `${i * 0.15}s`,
              })}
            />
          ))}
          {secondary.map((p, i) => (
            <line
              key={`s${i}`}
              x1={r2(p.inner[0])}
              y1={r2(p.inner[1])}
              x2={r2(p.outer[0])}
              y2={r2(p.outer[1])}
              strokeWidth={0.64}
              opacity={0.5}
              style={animStyle(active, "mti_pulse", "1s", {
                timing: "ease-in-out",
                delay: `${i * 0.1}s`,
              })}
            />
          ))}
          {primary.map((p, i) => (
            <polygon
              key={`d${i}`}
              points={diamond(r2(p.outer[0]), r2(p.outer[1]))}
              {...fill}
              style={animStyle(active, "mti_spin", "2s", {
                origin: `${r2(p.outer[0])}px ${r2(p.outer[1])}px`,
              })}
            />
          ))}
          <circle
            cx="12"
            cy="12"
            r="2"
            fill="none"
            strokeWidth={0.4}
            strokeDasharray="1 2"
            style={animStyle(active, "mti_flow", "2s")}
          />
          <circle
            cx="12"
            cy="12"
            r="2.8"
            fill="none"
            strokeWidth={0.4}
            strokeDasharray="1 2"
            style={animStyle(active, "mti_flow_rev", "2s")}
          />
          <g
            style={animStyle(active, "mti_core", "0.9s", {
              timing: "ease-in-out",
              origin: "12px 12px",
            })}
          >
            <polygon points={starPts} {...fill} opacity={0.85} />
            <circle cx="12" cy="12" r="1.5" {...fill} />
          </g>
        </>
      );
    }

    case "max": {
      // Sacred geometry (apex tier) — outer ring + a slowly rotating dashed
      // inner ring + a counter-rotating Star of David, a twinkling core, and 6
      // particles orbiting the centre 60° apart (negative delays phase them
      // evenly). The whole sigil breathes.
      const phases = [0, 1, 2, 3, 4, 5];
      const orbitDur = 4;
      return (
        <g style={animStyle(active, "mti_breathe_soft", "3s", { timing: "ease-in-out", origin: "12px 12px" })}>
          <circle cx="12" cy="12" r="10" strokeWidth={0.8} />
          <circle
            cx="12"
            cy="12"
            r="6.4"
            strokeWidth={0.48}
            opacity={0.4}
            strokeDasharray="1 3"
            style={animStyle(active, "mti_spin", "12s", { origin: "12px 12px" })}
          />
          <polygon
            points="12,3 19.79,16.5 4.21,16.5"
            style={animStyle(active, "mti_spin", "9s", { origin: "12px 12px" })}
          />
          <polygon
            points="12,21 4.21,7.5 19.79,7.5"
            style={animStyle(active, "mti_spin_rev", "9s", { origin: "12px 12px" })}
          />
          <circle
            cx="12"
            cy="12"
            r="2"
            {...fill}
            style={animStyle(active, "mti_pulse", "2s", { timing: "ease-in-out" })}
          />
          {phases.map((p) => (
            <circle
              key={p}
              cx="12"
              cy="12"
              r="0.9"
              {...fill}
              style={animStyle(active, "mti_orbit5", `${orbitDur}s`, {
                delay: `${-(p * orbitDur) / 6}s`,
              })}
            />
          ))}
        </g>
      );
    }
  }
}

export interface ModelTierIconProps {
  inputPricePerMillion: number;
  size?: number;
  showLabel?: boolean;
  // Force the animated state regardless of hover. Used by showcase/test
  // surfaces; production callers leave this undefined and animation is
  // hover-driven (so a list of 20+ icons stays static until pointed at).
  animate?: boolean;
}

export function ModelTierIcon({
  inputPricePerMillion,
  size = 20,
  showLabel = false,
  animate,
}: ModelTierIconProps) {
  const info = getTierInfo(inputPricePerMillion);
  const [hovered, setHovered] = useState(false);
  const active = animate === true || hovered;

  const glow = active ? GLOW_INTENSITY[info.icon] : 4;

  return (
    <span
      className="group inline-flex items-center gap-1.5"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <style>{KEYFRAMES}</style>
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke={info.color}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        style={{
          filter: `drop-shadow(0 0 ${glow}px ${info.color})`,
          transition: "filter 0.8s ease-in-out",
        }}
      >
        {renderGlyph(info.icon, info.color, active)}
      </svg>

      {showLabel ? (
        <span className="text-xs font-medium" style={{ color: info.color }}>
          {info.name}
        </span>
      ) : null}
    </span>
  );
}

export default ModelTierIcon;
