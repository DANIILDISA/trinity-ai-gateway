import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatFeedSkeleton } from "./ChatFeedSkeleton";

describe("ChatFeedSkeleton", () => {
  it("renders shimmer placeholder rows", () => {
    render(<ChatFeedSkeleton />);
    expect(screen.getByTestId("chat-feed-skeleton")).toBeInTheDocument();
    expect(
      screen.getByTestId("chat-feed-skeleton").querySelectorAll(".animate-shimmer").length,
    ).toBeGreaterThan(0);
  });
});
