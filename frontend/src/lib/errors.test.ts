import { describe, expect, it } from "vitest";
import { parseErrorBody } from "@/lib/errors";

// The exact 422 body the backend echoes back on an over-length message: the
// `input` field carries the ENTIRE pasted text (~99000 chars). The parser must
// surface ONLY details[].msg — never raw JSON, never the echoed input (tr-5zt).
const LONG_INPUT = "x".repeat(99000);
const VALIDATION_BODY = JSON.stringify({
  error: {
    code: "validation_error",
    message: "Ошибка валидации данных",
    details: [
      {
        type: "content_length",
        loc: ["body", "content"],
        msg: "Сообщение должно содержать от 1 до 32000 символов",
        input: LONG_INPUT,
      },
    ],
  },
});

describe("parseErrorBody (tr-5zt)", () => {
  it("returns the details[].msg for a 422 validation error, never raw JSON or the echoed input", () => {
    const { message, code } = parseErrorBody(VALIDATION_BODY, 422);
    expect(message).toBe("Сообщение должно содержать от 1 до 32000 символов");
    expect(code).toBe("validation_error");
    // Hard guarantees: no raw JSON, no echoed user input.
    expect(message).not.toContain("{");
    expect(message).not.toContain(LONG_INPUT);
    expect(message.length).toBeLessThan(500);
  });

  it("joins multiple details messages", () => {
    const body = JSON.stringify({
      error: {
        code: "validation_error",
        message: "Ошибка валидации данных",
        details: [
          { msg: "Поле A обязательно", input: "aaa" },
          { msg: "Поле B обязательно", input: "bbb" },
        ],
      },
    });
    const { message } = parseErrorBody(body, 422);
    expect(message).toContain("Поле A обязательно");
    expect(message).toContain("Поле B обязательно");
    expect(message).not.toContain("aaa");
    expect(message).not.toContain("bbb");
  });

  it("falls back to error.message when there are no details", () => {
    const body = JSON.stringify({
      error: { code: "openrouter_error", message: "Ошибка при обработке запроса" },
    });
    const { message, code } = parseErrorBody(body, 502);
    expect(message).toBe("Ошибка при обработке запроса");
    expect(code).toBe("openrouter_error");
  });

  it("falls back to a generic message for an unparseable body", () => {
    const { message } = parseErrorBody("<html>502 Bad Gateway</html>", 502);
    expect(message).toBe("Ошибка отправки сообщения");
    expect(message).not.toContain("<html>");
  });

  it("falls back to a generic message for an empty body", () => {
    const { message } = parseErrorBody("", 500);
    expect(message).toBe("Ошибка отправки сообщения");
  });

  it("ignores blank or non-string details msg entries", () => {
    const body = JSON.stringify({
      error: {
        code: "validation_error",
        message: "Ошибка валидации данных",
        details: [{ msg: "", input: "x" }, { input: "y" }],
      },
    });
    const { message } = parseErrorBody(body, 422);
    // No usable detail msg → fall back to the envelope message.
    expect(message).toBe("Ошибка валидации данных");
  });
});
