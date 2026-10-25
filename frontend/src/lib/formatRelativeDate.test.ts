import { describe, it, expect } from "vitest";

import { formatRelativeDate } from "./formatting";

// The MessageBubble user hover row shows the date only when it is NOT today
// (it guards on `formatRelativeDate(createdAt) !== "сегодня"`). These tests pin
// that contract: today → "сегодня" (omitted in the UI); earlier days → a short
// Russian relative/date label.
describe("formatRelativeDate", () => {
  const now = new Date(2026, 5, 8, 14, 30); // 2026-06-08, local time

  it("returns 'сегодня' for a timestamp earlier the same day", () => {
    const earlierToday = new Date(2026, 5, 8, 9, 0).toISOString();
    expect(formatRelativeDate(earlierToday, now)).toBe("сегодня");
  });

  it("returns 'вчера' for the previous calendar day", () => {
    const yesterday = new Date(2026, 5, 7, 23, 0).toISOString();
    expect(formatRelativeDate(yesterday, now)).toBe("вчера");
  });

  it("returns a short RU day count within the last week", () => {
    const threeDaysAgo = new Date(2026, 5, 5, 12, 0).toISOString();
    expect(formatRelativeDate(threeDaysAgo, now)).toBe("3 дня назад");
  });

  it("returns a short RU date for an older same-year timestamp", () => {
    const olderThisYear = new Date(2026, 0, 15, 12, 0).toISOString();
    expect(formatRelativeDate(olderThisYear, now)).toBe("15 янв");
  });

  it("appends the year for a different-year timestamp", () => {
    const lastYear = new Date(2025, 11, 31, 12, 0).toISOString();
    expect(formatRelativeDate(lastYear, now)).toBe("31 дек 2025");
  });

  it("echoes an unparseable input unchanged", () => {
    expect(formatRelativeDate("not-a-date", now)).toBe("not-a-date");
  });
});
