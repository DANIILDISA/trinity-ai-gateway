"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { ReconnectingScreen } from "@/components/ui/ReconnectingScreen";
import { Spinner } from "@/components/ui/Spinner";
import { ToastProvider } from "@/components/ui/Toast";
import { ToneProvider } from "@/components/ui/Tone";
import { useAuth } from "@/hooks/useAuth";

export default function MainLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const { isLoading, isAuthenticated, connectionError, retry } = useAuth();

  useEffect(() => {
    // Only a CONFIRMED logout routes to /login. A transient backend outage
    // (connectionError) must not — that was the spurious-logout bug (tr-lgm).
    if (!isLoading && !isAuthenticated && !connectionError) {
      router.replace("/login");
    }
  }, [isLoading, isAuthenticated, connectionError, router]);

  // Backend unreachable and we couldn't confirm the session: keep the user in
  // place and retry rather than bouncing them to the login page.
  if (!isAuthenticated && connectionError) {
    return <ReconnectingScreen onRetry={retry} />;
  }

  if (isLoading || !isAuthenticated) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Spinner size="lg" />
      </main>
    );
  }

  // Chat and Settings render their own chrome (sidebar / nested layout with
  // back link). Other (main) pages (balance, history) get a single slim back
  // link so they stay reachable now that the top header is gone.
  const isChat = pathname.startsWith("/chat");
  const isSettings = pathname.startsWith("/settings");
  const hideTopBar = isChat || isSettings;

  return (
    <ToneProvider tone="dark">
      <ToastProvider>
        <div className="flex min-h-screen flex-col bg-[var(--trinity-canvas)]">
          {hideTopBar ? null : (
            <div className="sticky top-0 z-10 border-b border-neutral-800/60 bg-neutral-950/95 backdrop-blur-sm">
              <div className="mx-auto flex h-12 max-w-5xl items-center px-4">
                <Link
                  href="/chat"
                  className="inline-flex items-center gap-1.5 text-sm text-neutral-400 transition-colors hover:text-neutral-100"
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M15 18l-6-6 6-6"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  К чату
                </Link>
              </div>
            </div>
          )}
          <div className="flex-1">{children}</div>
        </div>
      </ToastProvider>
    </ToneProvider>
  );
}
