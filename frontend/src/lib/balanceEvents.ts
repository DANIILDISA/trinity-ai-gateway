"use client";

// Cross-instance balance refresh (tr-hye6).
//
// The sidebar balance comes from one useAuth instance, the balance page from a
// separate useBalance instance, settings from yet another — a balance change in
// ONE flow (a top-up landing, a chat-send debit, an admin adjustment) must
// refresh them ALL immediately, without waiting for the 30s poll or a reload.
//
// A window event is the simplest cross-instance bus: every useAuth/useBalance
// subscribes on mount; any flow that changes the balance dispatches once.
const BALANCE_REFRESH_EVENT = "trinity:balance-refresh";

/** Signal every mounted balance consumer to refetch now. No-op on the server. */
export function dispatchBalanceRefresh(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(BALANCE_REFRESH_EVENT));
}

/** Subscribe to balance-refresh signals. Returns an unsubscribe fn. */
export function onBalanceRefresh(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(BALANCE_REFRESH_EVENT, handler);
  return () => window.removeEventListener(BALANCE_REFRESH_EVENT, handler);
}
