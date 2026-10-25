import { ToastProvider } from "@/components/ui/Toast";
import { ToneProvider } from "@/components/ui/Tone";

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <ToneProvider tone="dark">
      <main className="flex min-h-screen items-center justify-center bg-[var(--trinity-canvas)] px-4 py-12">
        <ToastProvider>
          <div className="w-full max-w-sm">{children}</div>
        </ToastProvider>
      </main>
    </ToneProvider>
  );
}
