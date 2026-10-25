"use client";

import { useState } from "react";

// Animated neural-network mark. Three layers of nodes (2→3→2) wired
// layer-to-layer, with a continuous, subtle emerald pulse travelling across
// the nodes and a soft glow. Transform/opacity-only so it composites on the
// GPU and never triggers layout. Self-contained <style> (identical rules
// dedupe across instances) since Tailwind can't register arbitrary keyframes
// at runtime.
//
// Two intensities:
// - idle: gentle breathing — appropriate for the sidebar logo + finished
//   assistant turns.
// - active (isStreaming): nodes scale hard and edges flicker brightly — used
//   on the assistant avatar while the model is generating, so the icon feels
//   alive while tokens are arriving.

// Animation amplitudes are intentionally damped ~30% vs. the original swing
// (scale deltas, opacity ranges shrunk toward their midpoints) for a calmer,
// subtler breathing. The icon's static size is unchanged.
const KEYFRAMES = `
@keyframes neural_pulse_idle {
  0%, 100% { opacity: 0.475; transform: scale(1); }
  50% { opacity: 0.9; transform: scale(1.175); }
}
@keyframes neural_pulse_active {
  0%, 100% { opacity: 0.66; transform: scale(1.1); }
  50% { opacity: 0.94; transform: scale(1.52); }
}
@keyframes neural_edge_idle {
  0%, 100% { opacity: 0.183; }
  50% { opacity: 0.437; }
}
@keyframes neural_edge_active {
  0%, 100% { opacity: 0.328; }
  50% { opacity: 0.922; }
}
`;

type Pt = readonly [number, number];

const LEFT: Pt[] = [
  [8, 14],
  [8, 26],
];
const MID: Pt[] = [
  [20, 8],
  [20, 20],
  [20, 32],
];
const RIGHT: Pt[] = [
  [32, 14],
  [32, 26],
];

const NODES: Pt[] = [...LEFT, ...MID, ...RIGHT];

// Fully-connected layer-to-layer edges.
const EDGES: { a: Pt; b: Pt }[] = [];
for (const l of LEFT) for (const m of MID) EDGES.push({ a: l, b: m });
for (const m of MID) for (const r of RIGHT) EDGES.push({ a: m, b: r });

export interface NeuralIconProps {
  size?: number;
  className?: string;
  /** Accent colour. Defaults to emerald-400. */
  color?: string;
  /** When true the animation intensifies — nodes scale harder and edges
   *  flicker faster/brighter. */
  isStreaming?: boolean;
}

export function NeuralIcon({
  size = 28,
  className = "",
  color = "#34d399",
  isStreaming = false,
}: NeuralIconProps) {
  // Hover lights the icon up the same way streaming does: animation switches
  // to the active keyframes and the glow halo expands. Streaming-while-hovered
  // simply stays at the maxed-out state — no further escalation needed.
  const [hovered, setHovered] = useState(false);
  const active = isStreaming || hovered;
  const nodeAnim = active ? "neural_pulse_active" : "neural_pulse_idle";
  const nodeDur = active ? "0.85s" : "2.4s";
  const edgeAnim = active ? "neural_edge_active" : "neural_edge_idle";
  const edgeDur = active ? "0.55s" : "3s";
  // Hover lays a thicker emerald halo on top of the streaming glow so the
  // interaction reads as a deliberate "lean in" rather than a brightness blip.
  const glowColor = active ? `${color}cc` : `${color}66`;
  const glowRadius = hovered ? 10 : isStreaming ? 6 : 3;

  return (
    <span
      className={`inline-flex ${className}`}
      aria-hidden="true"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <style>{KEYFRAMES}</style>
      <svg
        width={size}
        height={size}
        viewBox="0 0 40 40"
        fill="none"
        style={{
          filter: `drop-shadow(0 0 ${glowRadius}px ${glowColor})`,
          transition: "filter 0.4s ease-out",
        }}
      >
        {EDGES.map((e, i) => (
          <line
            key={`e${i}`}
            x1={e.a[0]}
            y1={e.a[1]}
            x2={e.b[0]}
            y2={e.b[1]}
            stroke={color}
            strokeWidth={active ? 1.0 : 0.85}
            style={{
              animation: `${edgeAnim} ${edgeDur} ease-in-out ${(i % 6) * 0.18}s infinite`,
              transition: "stroke-width 0.4s ease-out",
            }}
          />
        ))}
        {NODES.map((n, i) => (
          <circle
            key={`n${i}`}
            cx={n[0]}
            cy={n[1]}
            r={2.4}
            fill={color}
            style={{
              transformBox: "view-box",
              transformOrigin: `${n[0]}px ${n[1]}px`,
              animation: `${nodeAnim} ${nodeDur} ease-in-out ${i * 0.18}s infinite`,
            }}
          />
        ))}
      </svg>
    </span>
  );
}

export default NeuralIcon;
