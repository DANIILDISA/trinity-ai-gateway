import { describe, it, expect, beforeEach } from "vitest";
import { register, getRenderer, clearRegistry } from "./registry";

const Dummy = () => null;

describe("thinking registry", () => {
  beforeEach(() => clearRegistry());

  it("returns a registered renderer by kind", () => {
    register("reasoning", Dummy);
    expect(getRenderer("reasoning")).toBe(Dummy);
  });

  it("returns undefined for an unknown kind", () => {
    expect(getRenderer("nope")).toBeUndefined();
  });

  it("overwrites a renderer when re-registered", () => {
    const A = () => null;
    const B = () => null;
    register("tool_call", A);
    register("tool_call", B);
    expect(getRenderer("tool_call")).toBe(B);
  });
});
