import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { HomeSection, type HomeSectionProps } from "./HomeSection";
import type { User } from "@/types/api";

function renderHome(overrides: Partial<HomeSectionProps> = {}) {
  const props: HomeSectionProps = {
    user: {
      email: "a@b.ru",
      display_name: "Дисп",
      full_name: "Сохранённое Имя",
    } as unknown as User,
    displayName: "Дисп",
    displayNameError: null,
    isSavingDisplayName: false,
    displayNameSavedAt: null,
    onDisplayNameChange: vi.fn(),
    onSaveDisplayName: vi.fn(),
    fullName: "Сохранённое Имя",
    fullNameError: null,
    isSavingFullName: false,
    fullNameSavedAt: null,
    onFullNameChange: vi.fn(),
    onSaveFullName: vi.fn(),
    appearance: "dark",
    onAppearanceChange: vi.fn(),
    notifications: [],
    isLoadingNotifications: false,
    notificationPrefs: {},
    onToggleNotificationPref: vi.fn(),
    onMarkAsRead: vi.fn(),
    showPasswordSection: false,
    newPassword: "",
    confirmPassword: "",
    passwordError: null,
    isSavingPassword: false,
    passwordSuccess: false,
    onNewPasswordChange: vi.fn(),
    onConfirmPasswordChange: vi.fn(),
    onSetPassword: vi.fn(),
    ...overrides,
  };
  render(<HomeSection {...props} />);
  return props;
}

describe("HomeSection profile", () => {
  it("derives the avatar from the SAVED user, not the in-progress input (tr-uxkp)", () => {
    renderHome({
      // The user is mid-typing an over-limit name that has NOT been saved.
      fullName: "Печатаю Прямо Сейчас",
      user: {
        email: "a@b.ru",
        display_name: "Дисп",
        full_name: "Сохранённое Имя",
      } as unknown as User,
    });
    // Avatar = initials of the SAVED full_name "Сохранённое Имя" => "СИ",
    // never the unsaved input "Печатаю Прямо…" => "ПП".
    expect(screen.getByText("СИ")).toBeInTheDocument();
    expect(screen.queryByText("ПП")).not.toBeInTheDocument();
  });

  it("shows an inline error on the full-name field (tr-uxkp)", () => {
    renderHome({ fullNameError: "Не более 150 символов." });
    expect(screen.getByText("Не более 150 символов.")).toBeInTheDocument();
  });
});
