"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { AdminSidebar } from "@/components/admin/AdminSidebar";
import { Spinner } from "@/components/ui/Spinner";
import { useAuth } from "@/hooks/useAuth";
import { getAlerts } from "@/lib/adminApi";

const ALERTS_POLL_MS = 60_000;

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? "/admin";
  const { user, isLoading, isAuthenticated } = useAuth();

  const [alertsBadge, setAlertsBadge] = useState(0);

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated) {
      router.replace("/login");
      return;
    }
    if (user && user.role !== "admin") {
      router.replace("/chat");
    }
  }, [isLoading, isAuthenticated, user, router]);

  const isAdmin = isAuthenticated && user?.role === "admin";

  // Poll the unread alert count for the sidebar badge while in the admin
  // panel. Cheap call (per_page=1), updates the badge irrespective of which
  // admin page the operator is on.
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    const tick = () => {
      getAlerts({ status: "new", per_page: 1 })
        .then((res) => {
          if (!cancelled) setAlertsBadge(res.unread_count);
        })
        .catch(() => undefined);
    };
    tick();
    const id = setInterval(tick, ALERTS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [isAdmin]);

  if (isLoading || !isAdmin) {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Spinner size="lg" />
      </main>
    );
  }

  return (
    // Admin is a self-contained light theme. Declare its own dark text baseline
    // here so content doesn't inherit the global dark-canvas text color
    // (text-neutral-100 on <body>), which would render near-white on the light
    // bg-neutral-50 surface. The dark AdminSidebar sets its own colors and is
    // unaffected.
    <div className="flex min-h-screen bg-neutral-50 text-neutral-900">
      <AdminSidebar
        currentPath={pathname}
        displayName={user?.display_name ?? ""}
        alertsBadge={alertsBadge}
      />
      <main className="flex-1 overflow-x-auto px-6 py-6">{children}</main>
    </div>
  );
}
