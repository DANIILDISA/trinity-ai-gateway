import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Spies shared into the hoisted module mocks.
const mocks = vi.hoisted(() => ({
  saveBlob: vi.fn(),
  downloadToolArtifact: vi.fn(() => Promise.resolve()),
  base64ToBlob: vi.fn(() => new Blob(["bytes"])),
}));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "conv-1" }) }));
vi.mock("@/lib/downloadToolArtifact", () => ({
  saveBlob: mocks.saveBlob,
  downloadToolArtifact: mocks.downloadToolArtifact,
  fetchToolArtifact: vi.fn(),
}));
vi.mock("@/lib/binaryBlob", () => ({ base64ToBlob: mocks.base64ToBlob }));

import { RichResults } from "./richResults";

describe("FileDownloadChip — generated workspace file (tr-an0g.5)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows filename + human size and downloads inline base64 live", () => {
    render(
      <RichResults
        results={[
          { type: "file", filename: "data.csv", media_type: "text/csv", size: 2048, data: "YQ==" },
        ]}
      />,
    );
    expect(screen.getByText("data.csv")).toBeTruthy();
    expect(screen.getByText("2 КБ")).toBeTruthy(); // formatFileSize(2048)
    fireEvent.click(screen.getByRole("button"));
    // Live: the streamed base64 is decoded to real bytes and saved (no re-fetch).
    expect(mocks.base64ToBlob).toHaveBeenCalledWith("YQ==", "text/csv");
    expect(mocks.saveBlob).toHaveBeenCalled();
    expect(mocks.downloadToolArtifact).not.toHaveBeenCalled();
  });

  it("re-fetches from the linked artifact on reload (no inline data)", () => {
    render(
      <RichResults
        results={[
          {
            type: "file",
            filename: "out.xlsx",
            media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size: 100,
            artifact_id: "a1",
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(mocks.downloadToolArtifact).toHaveBeenCalledWith("conv-1", "out.xlsx");
    expect(mocks.saveBlob).not.toHaveBeenCalled();
  });

  it("renders disabled and does nothing when over-quota (no data, no artifact)", () => {
    render(
      <RichResults
        results={[{ type: "file", filename: "huge.csv", size: 999, unavailable: true }]}
      />,
    );
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(mocks.downloadToolArtifact).not.toHaveBeenCalled();
    expect(mocks.saveBlob).not.toHaveBeenCalled();
  });
});
