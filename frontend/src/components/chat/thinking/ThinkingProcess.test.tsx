import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingProcess } from "./index";
import type { ThinkingProcess as ThinkingProcessType } from "@/types/thinking";

// Live reasoning text now reveals on a rAF loop (useStreamReveal). jsdom doesn't
// advance rAF, so progressive reveal would leave the text empty on the assertion
// tick. Report reduced-motion (matchMedia.matches:true) so useStreamReveal snaps
// straight to the full text — these tests assert content/structure, not the
// reveal animation itself (which RevealMarkdown/useStreamReveal cover directly).
beforeEach(() => {
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: true,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }));
});
afterEach(() => vi.unstubAllGlobals());

const doneProcess: ThinkingProcessType = {
  segments: [{ kind: "reasoning", id: "reasoning", text: "my private reasoning" }],
  isActive: false,
  durationMs: 6000,
  reasoningTokens: 1200,
};

describe("ThinkingProcess", () => {
  it("renders nothing when inactive and empty", () => {
    const { container } = render(
      <ThinkingProcess process={{ segments: [], isActive: false }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the active state and force-opens while thinking", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [{ kind: "reasoning", id: "reasoning", text: "live" }],
          isActive: true,
          startedAt: Date.now(),
        }}
      />,
    );
    expect(screen.getByText(/Размышляю/)).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
  });

  it("is collapsed by default when done, expands on click", () => {
    // The body stays in the DOM (collapse is an animated grid-rows transition),
    // so collapse state is tracked via aria-expanded, not presence.
    render(<ThinkingProcess process={doneProcess} />);
    const header = screen.getByRole("button", { name: /Размышление/ });
    expect(header).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("my private reasoning")).toBeInTheDocument();
  });

  it("shows duration and token meta in the collapsed header", () => {
    render(<ThinkingProcess process={doneProcess} />);
    expect(screen.getByText(/6с · 1\.2k токенов/)).toBeInTheDocument();
  });

  it("re-collapses when the header is clicked again", () => {
    render(<ThinkingProcess process={doneProcess} />);
    const header = screen.getByRole("button", { name: /Размышление/ });
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
  });

  it("skips segment kinds with no registered renderer", () => {
    const process = {
      segments: [
        { kind: "reasoning", id: "r", text: "shown" },
        { kind: "mystery", id: "m", text: "hidden" },
      ],
      isActive: true,
      startedAt: Date.now(),
    } as unknown as ThinkingProcessType;
    render(<ThinkingProcess process={process} />);
    expect(screen.getByText("shown")).toBeInTheDocument();
    expect(screen.queryByText("hidden")).toBeNull();
  });
});

describe("ThinkingProcess — multi-segment timeline (tr-cy0.3.12)", () => {
  const multi: ThinkingProcessType = {
    segments: [
      { kind: "reasoning", id: "seg-0", text: "a", summary: "Анализирую" },
      { kind: "reasoning", id: "seg-1", text: "b", summary: "Готовлю ответ" },
    ],
    isActive: false,
    durationMs: 6000,
    reasoningTokens: 1200,
  };

  it("renders an overall collapse header (default EXPANDED) plus per-segment headers (tr-cy0.3.18)", () => {
    render(<ThinkingProcess process={multi} />);
    // tr-cy0.3.18: there is now a whole-block «Размышление» collapse toggle...
    const header = screen.getByRole("button", { name: /^Размышление/ });
    // ...but it is EXPANDED by default, so the timeline + per-segment evidence
    // stays visible without a click (preserves the tr-ygx visibility intent).
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("button", { name: /Анализирую/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Готовлю ответ/ }),
    ).toBeInTheDocument();
  });

  it("carries the duration+token meta in the collapse header; the Готово node is bare (tr-cy0.3.18)", () => {
    render(<ThinkingProcess process={multi} />);
    // The meta lives on the header now (so the collapsed one-line is a real
    // summary), NOT duplicated on the Готово foot node.
    expect(
      screen.getByRole("button", { name: /Размышление.*1\.2k токенов/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Готово$/)).toBeInTheDocument();
    // The token meta appears exactly once (header), not also on Готово.
    expect(screen.getAllByText(/1\.2k токенов/)).toHaveLength(1);
  });

  it("collapses the whole block to one line when the header is clicked (tr-cy0.3.18)", () => {
    render(<ThinkingProcess process={multi} />);
    const header = screen.getByRole("button", { name: /^Размышление/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the live Размышляю indicator while active", () => {
    render(
      <ThinkingProcess
        process={{ ...multi, isActive: true, startedAt: Date.now() }}
      />,
    );
    expect(screen.getByText(/Размышляю/)).toBeInTheDocument();
  });
});

describe("ThinkingProcess — «Файл представлен» node (tr-cy0.2.11)", () => {
  const fileStep = {
    kind: "tool_call" as const,
    id: "tc-0-0",
    title: "Создание файла python_exam_prep.md",
    tool: "create_file",
    status: "done" as const,
    filename: "python_exam_prep.md",
  };

  it("renders «Файл представлен» before «Готово» when a file was presented (legacy path)", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "reasoning", text: "thinking" },
            fileStep,
          ],
          isActive: false,
          durationMs: 6000,
        }}
      />,
    );
    const presented = screen.getByText("Файл представлен");
    const done = screen.getByText(/^Готово/);
    expect(presented).toBeInTheDocument();
    expect(done).toBeInTheDocument();
    // «Файл представлен» precedes «Готово» on the rail.
    expect(
      presented.compareDocumentPosition(done) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders «Файл представлен» in the multi-segment path too", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "s0", text: "a", summary: "Анализирую" },
            fileStep,
          ],
          isActive: false,
          durationMs: 6000,
        }}
      />,
    );
    expect(screen.getByText("Файл представлен")).toBeInTheDocument();
  });

  it("does NOT render «Файл представлен» when no file was presented", () => {
    render(<ThinkingProcess process={doneProcess} />);
    expect(screen.queryByText("Файл представлен")).toBeNull();
  });

  it("does NOT render «Файл представлен» while still thinking", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "reasoning", text: "thinking" },
            fileStep,
          ],
          isActive: true,
          startedAt: Date.now(),
        }}
      />,
    );
    expect(screen.queryByText("Файл представлен")).toBeNull();
  });

  it("surfaces tool steps inline by default — the collapse header is EXPANDED, not hiding the evidence (tr-ygx + tr-cy0.3.18)", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "reasoning", text: "обдумываю файл" },
            fileStep,
          ],
          isActive: false,
          durationMs: 6000,
        }}
      />,
    );
    // tr-cy0.3.18 adds a whole-block «Размышление» collapse toggle, but it must
    // default to EXPANDED so the agentic evidence (tool steps + foot nodes) stays
    // visible without a click — preserving the tr-ygx intent.
    const header = screen.getByRole("button", { name: /^Размышление/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    // The Done foot node renders (timeline is shown, not collapsed away).
    expect(screen.getByText(/^Готово/)).toBeInTheDocument();
  });

  it("does NOT render «Файл представлен» for a not-yet-done file step", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "reasoning", text: "thinking" },
            { ...fileStep, status: "running" as const },
          ],
          isActive: false,
          durationMs: 6000,
        }}
      />,
    );
    expect(screen.queryByText("Файл представлен")).toBeNull();
  });
});

describe("ThinkingProcess — stopped terminal node (tr-fo1)", () => {
  const fileStep = {
    kind: "tool_call" as const,
    id: "tc-0-0",
    title: "Создание файла report.md",
    tool: "create_file",
    status: "done" as const,
    filename: "report.md",
  };

  it("renders «Остановлено» instead of «Готово» on a stopped legacy reply", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "reasoning", text: "обрываю мысль" },
          ],
          isActive: false,
          durationMs: 6000,
          stopped: true,
        }}
      />,
    );
    expect(screen.getByText("Остановлено")).toBeInTheDocument();
    expect(screen.queryByText(/^Готово/)).toBeNull();
  });

  it("renders «Остановлено» instead of «Готово» on a stopped multi-segment reply", () => {
    render(
      <ThinkingProcess
        process={{
          segments: [
            { kind: "reasoning", id: "s0", text: "a", summary: "Анализирую" },
            fileStep,
          ],
          isActive: false,
          durationMs: 6000,
          reasoningTokens: 1200,
          stopped: true,
        }}
      />,
    );
    expect(screen.getByText(/Остановлено/)).toBeInTheDocument();
    expect(screen.queryByText(/^Готово/)).toBeNull();
  });

  it("keeps «Готово» on a completed reply (stopped falsy)", () => {
    render(<ThinkingProcess process={doneProcess} />);
    // Expand so the foot node is rendered.
    fireEvent.click(screen.getByRole("button", { name: /Размышление/ }));
    expect(screen.getByText(/^Готово/)).toBeInTheDocument();
    expect(screen.queryByText("Остановлено")).toBeNull();
  });
});
