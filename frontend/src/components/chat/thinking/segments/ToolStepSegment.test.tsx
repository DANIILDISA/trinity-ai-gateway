import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ToolStepSegment } from "./ToolStepSegment";
import type { ToolCallSegment } from "@/types/thinking";

const segment: ToolCallSegment = {
  kind: "tool_call",
  id: "s1",
  title: "Extract text from the uploaded docx file",
  tool: "Script",
  output: "print('hi')",
  status: "done",
};

describe("ToolStepSegment — disclosure (tr-cy0.2.12)", () => {
  it("is expanded by default and shows the step detail", () => {
    render(<ToolStepSegment segment={segment} />);
    expect(
      screen.getByRole("button", { name: /Extract text/ }),
    ).toHaveAttribute("aria-expanded", "true");
    // The chip (friendly title, no filename here) is part of the revealed body.
    // It falls back to the step title, never the raw tool name (tr-8bj).
    expect(screen.getAllByText(/Extract text/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Script")).toBeNull();
  });

  it("collapses and re-expands when the header chevron is toggled", () => {
    render(<ToolStepSegment segment={segment} />);
    const header = screen.getByRole("button", { name: /Extract text/ });
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the tool output inside the disclosure body", () => {
    render(<ToolStepSegment segment={segment} />);
    expect(screen.getByText("print('hi')")).toBeInTheDocument();
  });

  it("renders no output block when the step has no output", () => {
    render(<ToolStepSegment segment={{ ...segment, output: undefined }} />);
    expect(screen.queryByText("print('hi')")).toBeNull();
  });

  // tr-cy0.3.19: code output (e.g. a viewed file) must render like the main
  // answer's code blocks — a dark highlighted panel — not muted plain text.
  it("renders code output as a highlighted code panel, not muted text-neutral-500", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{
          ...segment,
          filename: "calc.py",
          output: "def f():\n    return 1",
          status: "done",
        }}
      />,
    );
    // The dark code panel (same surface as the main answer's CodeBlock) wraps it.
    expect(container.querySelector('[class*="161615"]')).not.toBeNull();
    // The code text is rendered bright (CodeHighlight fallback), not the old
    // muted gray <pre>.
    const codeEl = screen.getByText(/def f\(\):/);
    expect(codeEl.className).not.toContain("text-neutral-500");
  });

  it("keeps an ERROR output as muted red plain text (no code panel)", () => {
    const { container } = render(
      <ToolStepSegment
        segment={{ ...segment, output: "Boom: not found", status: "error" }}
      />,
    );
    expect(screen.getByText(/Boom/).className).toContain("text-red-400");
    expect(container.querySelector('[class*="161615"]')).toBeNull();
  });

  it("renders a pulse for a running step", () => {
    const { container } = render(
      <ToolStepSegment segment={{ ...segment, status: "running" }} />,
    );
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
  });

  it("exposes the body via aria-controls for keyboard / screen-reader users", () => {
    render(<ToolStepSegment segment={segment} />);
    const header = screen.getByRole("button", { name: /Extract text/ });
    expect(header).toHaveAttribute("aria-controls");
  });
});

describe("ToolStepSegment — filename chip + diff (tr-cy0.2.10)", () => {
  const fileStep: ToolCallSegment = {
    kind: "tool_call",
    id: "tc-0-0",
    title: "Правка файла python_exam_prep.md",
    tool: "str_replace",
    filename: "python_exam_prep.md",
    diff: { added: 2188, removed: 1 },
    status: "done",
  };

  it("shows the filename in the chip (not the tool name)", () => {
    render(<ToolStepSegment segment={fileStep} />);
    expect(screen.getByText("python_exam_prep.md")).toBeInTheDocument();
    expect(screen.queryByText("str_replace")).toBeNull();
  });

  it("renders the inline diff counter +added and -removed", () => {
    render(<ToolStepSegment segment={fileStep} />);
    expect(screen.getByText("+2188")).toBeInTheDocument();
    expect(screen.getByText("-1")).toBeInTheDocument();
  });

  it("omits the removed counter when removed is 0 (create_file)", () => {
    const created: ToolCallSegment = {
      ...fileStep,
      tool: "create_file",
      diff: { added: 42, removed: 0 },
    };
    render(<ToolStepSegment segment={created} />);
    expect(screen.getByText("+42")).toBeInTheDocument();
    expect(screen.queryByText(/^-\d/)).toBeNull();
  });

  it("toggles the group via the header (aria-expanded)", () => {
    render(<ToolStepSegment segment={fileStep} />);
    const header = screen.getByRole("button", { name: /Правка/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
  });
});

describe("ToolStepSegment — states (tr-cy0.2.14)", () => {
  const errored: ToolCallSegment = {
    kind: "tool_call",
    id: "tc-0-0",
    title: "Создание файла python_exam_prep.md",
    tool: "create_file",
    filename: "python_exam_prep.md",
    output: "файл существует, используйте str_replace",
    status: "error",
  };

  it("marks an errored step with an accessible error indicator (not a pulse)", () => {
    const { container } = render(<ToolStepSegment segment={errored} />);
    expect(screen.getByLabelText("Ошибка")).toBeInTheDocument();
    expect(container.querySelector(".animate-pulse")).toBeNull();
  });

  it("surfaces the error message in the body", () => {
    render(<ToolStepSegment segment={errored} />);
    expect(
      screen.getByText("файл существует, используйте str_replace"),
    ).toBeInTheDocument();
  });

  it("shows no error indicator for a done step", () => {
    render(<ToolStepSegment segment={{ ...errored, status: "done" }} />);
    expect(screen.queryByLabelText("Ошибка")).toBeNull();
  });
});

describe("ToolStepSegment — interrupted step without filename (tr-8bj)", () => {
  // A hard Stop landed while create_file args were still streaming, so the path
  // never arrived → no `filename`. The chip must fall back to the friendly RU
  // `title` («Создание файла»), NOT leak the raw tool name «create_file».
  const interrupted: ToolCallSegment = {
    kind: "tool_call",
    id: "tc-0-0",
    title: "Создание файла",
    tool: "create_file",
    status: "error",
  };

  it("shows the friendly title in the chip, not the raw tool name", () => {
    render(<ToolStepSegment segment={interrupted} />);
    expect(screen.getAllByText("Создание файла").length).toBeGreaterThan(0);
    expect(screen.queryByText("create_file")).toBeNull();
  });
});

describe("ToolStepSegment — long names / a11y (tr-cy0.2.15)", () => {
  const longStep: ToolCallSegment = {
    kind: "tool_call",
    id: "tc-0-0",
    title:
      "Создание очень длинного markdown файла с подробным разбором всех вопросов",
    tool: "create_file",
    filename: "очень_длинное_имя_файла_подготовка_к_экзамену.md",
    status: "done",
  };

  it("keeps the full title recoverable via a native title tooltip when truncated", () => {
    render(<ToolStepSegment segment={longStep} />);
    expect(screen.getByTitle(longStep.title)).toBeInTheDocument();
  });

  it("truncates the title (min-w-0 + truncate) so a long RU title never overflows", () => {
    render(<ToolStepSegment segment={longStep} />);
    const titleSpan = screen.getByText(longStep.title);
    expect(titleSpan.className).toContain("truncate");
    expect(titleSpan.className).toContain("min-w-0");
  });

  it("keeps the full filename recoverable via the chip's title tooltip", () => {
    render(<ToolStepSegment segment={longStep} />);
    expect(screen.getByTitle(longStep.filename!)).toBeInTheDocument();
  });
});
