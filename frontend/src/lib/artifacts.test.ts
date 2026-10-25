import { describe, it, expect } from "vitest";

import { extractArtifacts, artifactExtension, artifactLabel } from "./artifacts";

const md = (...lines: string[]) => lines.join("\n");

describe("extractArtifacts", () => {
  it("lifts a fenced markdown block into one artifact and keeps the surrounding prose", () => {
    const response = md(
      "Готово! Вот что получилось:",
      "",
      "# Python_exam_prep.md",
      "",
      "```markdown",
      "# Подготовка к экзамену по Python",
      "",
      "| Цикл | Когда |",
      "| --- | --- |",
      "| for | известно число |",
      "",
      "Описание циклов и примеры.",
      "```",
      "",
      "Дай обратную связь по этому файлу.",
    );

    const { stripped, artifacts } = extractArtifacts(response);

    expect(artifacts).toHaveLength(1);
    expect(artifacts[0].kind).toBe("markdown");
    expect(artifacts[0].content).toContain("Подготовка к экзамену по Python");
    // Explanatory prose stays in the stream…
    expect(stripped).toContain("Готово! Вот что получилось:");
    expect(stripped).toContain("Дай обратную связь по этому файлу.");
    // …but the extracted document does not.
    expect(stripped).not.toContain("| --- |");
    expect(stripped).not.toContain("Подготовка к экзамену по Python");
  });

  it("derives the title from a preceding filename heading (underscores + extension stripped)", () => {
    const response = md(
      "# Python_exam_prep.md",
      "",
      "```markdown",
      "# Подготовка к экзамену по Python",
      "содержимое",
      "```",
    );
    const { artifacts } = extractArtifacts(response);
    expect(artifacts[0].title).toBe("Python exam prep");
  });

  it("does NOT extract short inline code blocks below the thresholds", () => {
    const response = md("Пример:", "", "```js", "const a = 1;", "```", "", "Конец.");
    const { stripped, artifacts } = extractArtifacts(response);
    expect(artifacts).toHaveLength(0);
    expect(stripped).toBe(response);
  });

  it("extracts a large code block as a code artifact with its language", () => {
    const body = Array.from({ length: 20 }, (_, i) => `line_${i} = ${i}`).join("\n");
    const response = md("Скрипт:", "", "```python", body, "```");
    const { artifacts } = extractArtifacts(response);
    expect(artifacts).toHaveLength(1);
    expect(artifacts[0].kind).toBe("code");
    expect(artifacts[0].language).toBe("python");
  });

  it("returns multiple artifacts in order of appearance", () => {
    const big = (tag: string) => Array.from({ length: 18 }, (_, i) => `${tag}_${i}`).join("\n");
    const response = md("```python", big("a"), "```", "", "между", "", "```sql", big("b"), "```");
    const { artifacts } = extractArtifacts(response);
    expect(artifacts.map((a) => a.language)).toEqual(["python", "sql"]);
  });

  it("handles a 4-backtick markdown doc that contains an inner 3-backtick code fence", () => {
    const response = md(
      "Вот файл:",
      "",
      "````markdown",
      "# Doc",
      "```python",
      "print(1)",
      "```",
      "| a | b |",
      "| - | - |",
      "````",
      "",
      "Готово.",
    );
    const { stripped, artifacts } = extractArtifacts(response);
    expect(artifacts).toHaveLength(1);
    expect(artifacts[0].kind).toBe("markdown");
    expect(artifacts[0].content).toContain("print(1)");
    expect(stripped).toContain("Вот файл:");
    expect(stripped).toContain("Готово.");
    expect(stripped).not.toContain("print(1)");
  });

  it("produces a deterministic id: stable for equal content, distinct for different content", () => {
    const a1 = extractArtifacts(md("```markdown", "# A", "x", "```")).artifacts[0];
    const a2 = extractArtifacts(md("```markdown", "# A", "x", "```")).artifacts[0];
    const b = extractArtifacts(md("```markdown", "# B", "y", "```")).artifacts[0];
    expect(a1.id).toBe(a2.id);
    expect(a1.id).not.toBe(b.id);
  });

  it("can be skipped (server artifacts take over)", () => {
    const response = md("```markdown", "# A", "x", "```");
    const { stripped, artifacts } = extractArtifacts(response, { skip: true });
    expect(artifacts).toHaveLength(0);
    expect(stripped).toBe(response);
  });
});

describe("artifact label/extension helpers", () => {
  it("labels and names a markdown artifact", () => {
    const [a] = extractArtifacts(md("```markdown", "# A", "y", "```")).artifacts;
    expect(artifactLabel(a)).toBe("Документ · MD");
    expect(artifactExtension(a)).toBe(".md");
  });

  it("labels and names a code artifact by language", () => {
    const body = Array.from({ length: 20 }, (_, i) => `x${i}=${i}`).join("\n");
    const [a] = extractArtifacts(md("```python", body, "```")).artifacts;
    expect(artifactLabel(a)).toBe("Код · PYTHON");
    expect(artifactExtension(a)).toBe(".py");
  });
});
