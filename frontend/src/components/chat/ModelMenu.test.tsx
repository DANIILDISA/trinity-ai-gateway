import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { ModelMenu } from "./ModelMenu";
import type { ModelInfo, ThinkingEffort } from "@/types/api";

const model = (over: Partial<ModelInfo> = {}): ModelInfo => ({
  id: "m1",
  name: "Claude Haiku",
  provider: "anthropic",
  input_price_per_m: "100",
  output_price_per_m: "300",
  context_window: 200000,
  status: "active",
  supports_image_input: false,
  supports_file_input: false,
  supports_tools: true,
  supports_reasoning: false,
  supports_web_search: false,
  ...over,
});

const baseProps = () => ({
  selectedModel: model(),
  disabled: false,
  thinkingEnabled: false,
  thinkingEffort: "medium" as ThinkingEffort,
  toolsEnabled: false,
  onOpenModelSelector: vi.fn(),
  onToggleThinking: vi.fn(),
  onSetThinkingEffort: vi.fn(),
  onToggleTools: vi.fn(),
});

// Open the menu by clicking the model-name trigger, then return the Инструменты
// row. The whole row is the control now (role=menuitemcheckbox), so the visible
// toggle is decorative and the row itself carries the checked state + click.
function openAndGetToolsRow() {
  fireEvent.click(screen.getByRole("button", { name: /Claude Haiku/ }));
  return screen.getByRole("menuitemcheckbox", { name: "Инструменты" });
}

describe("ModelMenu — Инструменты toggle gating (tr-cy0.2.17 / D3)", () => {
  it("disables the toggle on a model that does not support tools", () => {
    render(
      <ModelMenu
        {...baseProps()}
        selectedModel={model({ supports_tools: false })}
      />,
    );
    expect(openAndGetToolsRow()).toBeDisabled();
  });

  it("enables the toggle and calls onToggleTools on a supporting model", () => {
    const props = baseProps();
    render(<ModelMenu {...props} />);
    const sw = openAndGetToolsRow();
    expect(sw).toBeEnabled();
    fireEvent.click(sw);
    expect(props.onToggleTools).toHaveBeenCalledTimes(1);
  });

  it("shows OFF when tools are enabled but the model lacks support (effectiveTools)", () => {
    render(
      <ModelMenu
        {...baseProps()}
        selectedModel={model({ supports_tools: false })}
        toolsEnabled
      />,
    );
    expect(openAndGetToolsRow()).toHaveAttribute("aria-checked", "false");
  });

  it("shows ON when tools are enabled on a supporting model", () => {
    render(<ModelMenu {...baseProps()} toolsEnabled />);
    expect(openAndGetToolsRow()).toHaveAttribute("aria-checked", "true");
  });
});

// tr-34g (O-6): toggling Thinking then immediately Инструменты in the SAME open
// menu. ModelMenu is controlled, so the real-app re-render path is reproduced by
// a stateful harness that flips thinkingEnabled/toolsEnabled and re-renders the
// menu. The menu's open state is ModelMenu-local, so a parent re-render must NOT
// close/remount it — both consecutive toggles must register.
describe("ModelMenu — consecutive toggles survive re-render (tr-34g / O-6)", () => {
  function Harness({
    onThinking,
    onTools,
  }: {
    onThinking: () => void;
    onTools: () => void;
  }) {
    const [thinkingEnabled, setThinkingEnabled] = useState(false);
    const [toolsEnabled, setToolsEnabled] = useState(false);
    return (
      <ModelMenu
        selectedModel={model({ supports_reasoning: true, supports_tools: true })}
        disabled={false}
        thinkingEnabled={thinkingEnabled}
        thinkingEffort="medium"
        toolsEnabled={toolsEnabled}
        onOpenModelSelector={vi.fn()}
        onToggleThinking={() => {
          setThinkingEnabled((v) => !v);
          onThinking();
        }}
        onSetThinkingEffort={vi.fn()}
        onToggleTools={() => {
          setToolsEnabled((v) => !v);
          onTools();
        }}
      />
    );
  }

  it("registers a 2nd toggle (Инструменты) clicked right after the 1st (Thinking)", () => {
    const onThinking = vi.fn();
    const onTools = vi.fn();
    render(<Harness onThinking={onThinking} onTools={onTools} />);

    // Open the menu once; both switches share one open session.
    fireEvent.click(screen.getByRole("button", { name: /Claude Haiku/ }));

    // 1st toggle — flips thinkingEnabled and re-renders the menu (effort row
    // enables, thinking glyph turns on). The menu must stay open. The whole row
    // is the control (role=menuitemcheckbox), clicking anywhere on it toggles.
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Thinking" }));
    expect(onThinking).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    // 2nd toggle, immediately after, in the same menu. If the Инструменты row
    // had been remounted/re-keyed by the 1st toggle's re-render, this click would
    // hit a stale element and be lost — the original bug report.
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Инструменты" }));
    expect(onTools).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("menuitemcheckbox", { name: "Инструменты" }),
    ).toHaveAttribute("aria-checked", "true");
  });
});
