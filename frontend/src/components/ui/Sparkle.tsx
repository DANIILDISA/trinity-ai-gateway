"use client";

// Claude-style animated sparkle/burst. A multi-ray asterisk radiating from
// the centre — rays alternate between two lengths so the star reads as a
// soft, organic burst rather than a rigid wheel. The whole mark rotates
// slowly while a gentle opacity pulse breathes underneath, so the motion is
// alive but never distracting. Transform/opacity-only (GPU-composited, no
// layout). Keyframe names are file-local so they never collide with the
// NeuralIcon / ModelTierIcon marks.

const KEYFRAMES = `
@keyframes sparkle_spin { to { transform: rotate(360deg); } }
@keyframes sparkle_pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}
`;

const RAY_COUNT = 12;
// Outer radius the longest rays reach (viewBox is 0..40, centre at 20).
const LONG = 17;
const SHORT = 11;
const CENTER = 20;

// Pre-compute ray endpoints. Alternating long/short rays produce the
// signature "soft burst" look. Each ray is a line from the centre outward.
const RAYS = Array.from({ length: RAY_COUNT }, (_, i) => {
  const angle = (i / RAY_COUNT) * Math.PI * 2;
  const len = i % 2 === 0 ? LONG : SHORT;
  return {
    x2: CENTER + Math.cos(angle) * len,
    y2: CENTER + Math.sin(angle) * len,
  };
});

export interface SparkleProps {
  size?: number;
  className?: string;
  /** Accent colour. Defaults to brand cyan (#22d3ee). */
  color?: string;
}

export function Sparkle({
  size = 28,
  className = "",
  color = "#22d3ee",
}: SparkleProps) {
  return (
    <span className={`inline-flex ${className}`} aria-hidden="true">
      <style>{KEYFRAMES}</style>
      <svg
        width={size}
        height={size}
        viewBox="0 0 40 40"
        fill="none"
        style={{
          animation: "sparkle_spin 18s linear infinite, sparkle_pulse 3.2s ease-in-out infinite",
          transformOrigin: "20px 20px",
          filter: `drop-shadow(0 0 4px ${color}55)`,
        }}
      >
        {RAYS.map((r, i) => (
          <line
            key={i}
            x1={CENTER}
            y1={CENTER}
            x2={r.x2}
            y2={r.y2}
            stroke={color}
            strokeWidth={i % 2 === 0 ? 1.5 : 1.1}
            strokeLinecap="round"
          />
        ))}
        <circle cx={CENTER} cy={CENTER} r={2.2} fill={color} />
      </svg>
    </span>
  );
}

export default Sparkle;
