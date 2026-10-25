// Shimmer placeholder mimicking the feed while history loads. Themed to the live
// canvas (#1f1f1e) with neutral-800 bars; reuses the existing `animate-shimmer`
// keyframe. Static-ish under prefers-reduced-motion (acceptable; no JS branch).
const LINES = ["w-[92%]", "w-[80%]", "w-[88%]", "w-[64%]", "w-[84%]", "w-[72%]", "w-[90%]", "w-[58%]"];

function Bar({ width }: { width: string }) {
  return (
    <div className={`relative h-3.5 overflow-hidden rounded bg-neutral-800 ${width}`}>
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-neutral-700/60 to-transparent" />
    </div>
  );
}

export function ChatFeedSkeleton() {
  return (
    <div
      data-testid="chat-feed-skeleton"
      className="mx-auto flex w-full max-w-[768px] flex-col gap-6 px-4 py-6"
      aria-busy="true"
      aria-label="Загрузка диалога"
    >
      <div className="flex justify-end">
        <div className="relative h-10 w-48 overflow-hidden rounded-2xl bg-neutral-800">
          <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-neutral-700/60 to-transparent" />
        </div>
      </div>
      <div className="flex flex-col gap-3">
        {LINES.map((w, i) => (
          <Bar key={i} width={w} />
        ))}
      </div>
    </div>
  );
}
