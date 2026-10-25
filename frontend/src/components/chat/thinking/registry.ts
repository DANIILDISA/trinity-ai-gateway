import type { ComponentType } from "react";
import type { ThinkingSegment } from "@/types/thinking";

// A renderer for a specific segment kind receives exactly that narrowed segment.
export type SegmentRenderer<K extends ThinkingSegment["kind"]> = ComponentType<{
  segment: Extract<ThinkingSegment, { kind: K }>;
}>;

// Internally stored as a renderer of the full union; register() narrows at the
// call site, so the container only ever passes a matching segment.
type AnyRenderer = ComponentType<{ segment: ThinkingSegment }>;

const renderers = new Map<string, AnyRenderer>();

export function register<K extends ThinkingSegment["kind"]>(
  kind: K,
  component: SegmentRenderer<K>,
): void {
  renderers.set(kind, component as AnyRenderer);
}

export function getRenderer(kind: string): AnyRenderer | undefined {
  return renderers.get(kind);
}

// Test-only: reset registrations between cases.
export function clearRegistry(): void {
  renderers.clear();
}
