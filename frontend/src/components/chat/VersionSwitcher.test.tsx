import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { VersionSwitcher } from "./VersionSwitcher";

describe("VersionSwitcher", () => {
  it("renders the 1-based n/m label", () => {
    render(
      <VersionSwitcher index={0} count={3} onPrev={() => {}} onNext={() => {}} />,
    );
    expect(screen.getByText("1/3")).toBeInTheDocument();
  });

  it("renders nothing when there is only one version", () => {
    const { container } = render(
      <VersionSwitcher index={0} count={1} onPrev={() => {}} onNext={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("disables prev on the first version and next on the last", () => {
    const { rerender } = render(
      <VersionSwitcher index={0} count={3} onPrev={() => {}} onNext={() => {}} />,
    );
    expect(screen.getByLabelText("Предыдущая версия")).toBeDisabled();
    expect(screen.getByLabelText("Следующая версия")).not.toBeDisabled();

    rerender(
      <VersionSwitcher index={2} count={3} onPrev={() => {}} onNext={() => {}} />,
    );
    expect(screen.getByLabelText("Предыдущая версия")).not.toBeDisabled();
    expect(screen.getByLabelText("Следующая версия")).toBeDisabled();
  });

  it("invokes onPrev / onNext on the respective arrows", () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    render(
      <VersionSwitcher index={1} count={3} onPrev={onPrev} onNext={onNext} />,
    );
    fireEvent.click(screen.getByLabelText("Предыдущая версия"));
    fireEvent.click(screen.getByLabelText("Следующая версия"));
    expect(onPrev).toHaveBeenCalledTimes(1);
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  it("does not call onPrev when already at the first version", () => {
    const onPrev = vi.fn();
    render(
      <VersionSwitcher index={0} count={3} onPrev={onPrev} onNext={() => {}} />,
    );
    fireEvent.click(screen.getByLabelText("Предыдущая версия"));
    expect(onPrev).not.toHaveBeenCalled();
  });
});
