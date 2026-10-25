import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; stub it so components that observe element size
// (e.g. ThinkingBody's overflow measurement) don't throw under test.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!("ResizeObserver" in globalThis)) {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub;
}

// jsdom has no matchMedia; stub it (defaulting to "not matching") so components
// using useMediaQuery (e.g. ChatWindow's wide-canvas split) render under test.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as unknown as typeof window.matchMedia;
}
