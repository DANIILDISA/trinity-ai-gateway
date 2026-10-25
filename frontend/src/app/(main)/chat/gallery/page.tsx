"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { apiClient } from "@/lib/api";
import { formatDateTime } from "@/lib/formatting";
import { formatFileSize } from "@/lib/fileTypes";
import { downloadToolArtifact } from "@/lib/downloadToolArtifact";
import {
  FileViewerModal,
  downloadFile,
  isViewableKind,
  type FileTarget,
} from "@/components/chat/FileViewer";
import type {
  GalleryArtifact,
  GalleryArtifactsResponse,
  GalleryFile,
  GalleryFilesResponse,
  GalleryImage,
  GalleryResponse,
} from "@/types/api";

const PER_PAGE = 50;

// Files view has two modes. Default = «Загрузки»: the user's uploads/attachments
// (images + chat_files) — unchanged. Opt-in «Созданные ИИ»: model-authored
// artifacts (the `artifacts` table), loaded lazily only when the tab is opened.
type Mode = "uploads" | "artifacts";

// One cell of the unified uploads grid: either a persisted image or a file.
type Entry =
  | { kind: "image"; created_at: string; image: GalleryImage }
  | { kind: "file"; created_at: string; file: GalleryFile };

export default function FilesPage() {
  const [mode, setMode] = useState<Mode>("uploads");
  const [fileTarget, setFileTarget] = useState<FileTarget | null>(null);

  return (
    <div className="flex h-[calc(100vh-6.5rem)] flex-col rounded-lg border border-neutral-800 bg-neutral-900">
      <header className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight text-neutral-100">
            Файлы
          </h1>
          <ModeTabs mode={mode} onChange={setMode} />
        </div>
        <Link
          href="/chat"
          className="rounded border border-neutral-700 px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:bg-neutral-800 hover:border-neutral-600"
        >
          ← Назад
        </Link>
      </header>

      {mode === "uploads" ? (
        <UploadsView onOpenFile={setFileTarget} />
      ) : (
        <ArtifactsView />
      )}

      {fileTarget ? (
        <FileViewerModal target={fileTarget} onClose={() => setFileTarget(null)} />
      ) : null}
    </div>
  );
}

function ModeTabs({
  mode,
  onChange,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
}) {
  return (
    <div role="tablist" aria-label="Режим" className="mt-1 flex items-center gap-1">
      <ModeTab
        active={mode === "uploads"}
        onClick={() => onChange("uploads")}
        label="Загрузки"
      />
      <ModeTab
        active={mode === "artifacts"}
        onClick={() => onChange("artifacts")}
        label="Созданные ИИ"
      />
    </div>
  );
}

function ModeTab({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`rounded-md px-2 py-0.5 text-xs font-medium transition-colors ${
        active
          ? "bg-emerald-600/15 text-emerald-400"
          : "text-neutral-500 hover:text-neutral-300"
      }`}
    >
      {label}
    </button>
  );
}

// ── Uploads mode (the original Files view, behaviourally unchanged) ──────────

function UploadsView({
  onOpenFile,
}: {
  onOpenFile: (target: FileTarget) => void;
}) {
  const [images, setImages] = useState<GalleryImage[]>([]);
  const [files, setFiles] = useState<GalleryFile[]>([]);
  const [imagesTotal, setImagesTotal] = useState(0);
  const [filesTotal, setFilesTotal] = useState(0);
  const [page, setPage] = useState(0); // last loaded page (0 = none yet)
  const [imagesHasMore, setImagesHasMore] = useState(true);
  const [filesHasMore, setFilesHasMore] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // Guards against the IntersectionObserver firing a second load before the
  // first finishes (isLoading state lags a tick behind the callback).
  const loadingRef = useRef(false);

  const hasMore = imagesHasMore || filesHasMore;

  const loadNext = useCallback(async () => {
    if (loadingRef.current || !(imagesHasMore || filesHasMore)) return;
    loadingRef.current = true;
    setIsLoading(true);
    setError(null);
    const nextPage = page + 1;
    try {
      // Paginate both sources in lockstep; the unified grid is sorted by
      // created_at on render so ordering stays correct across both streams.
      const [imgRes, fileRes] = await Promise.all([
        imagesHasMore
          ? apiClient.get<GalleryResponse>(
              `/api/v1/images/gallery?page=${nextPage}&per_page=${PER_PAGE}`,
            )
          : Promise.resolve(null),
        filesHasMore
          ? apiClient.get<GalleryFilesResponse>(
              `/api/v1/files/gallery?page=${nextPage}&per_page=${PER_PAGE}`,
            )
          : Promise.resolve(null),
      ]);
      if (imgRes) {
        setImages((prev) => [...prev, ...imgRes.items]);
        setImagesTotal(imgRes.total);
        setImagesHasMore(imgRes.has_more);
      }
      if (fileRes) {
        setFiles((prev) => [...prev, ...fileRes.items]);
        setFilesTotal(fileRes.total);
        setFilesHasMore(fileRes.has_more);
      }
      setPage(nextPage);
    } catch {
      setError("Не удалось загрузить файлы.");
    } finally {
      loadingRef.current = false;
      setIsLoading(false);
    }
  }, [page, imagesHasMore, filesHasMore]);

  // Initial load.
  useEffect(() => {
    void loadNext();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Infinite scroll: load the next page when the sentinel enters the viewport.
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) void loadNext();
      },
      { rootMargin: "400px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadNext]);

  const entries: Entry[] = [
    ...images.map(
      (image): Entry => ({ kind: "image", created_at: image.created_at, image }),
    ),
    ...files.map(
      (file): Entry => ({ kind: "file", created_at: file.created_at, file }),
    ),
  ].sort((a, b) => b.created_at.localeCompare(a.created_at));

  const total = imagesTotal + filesTotal;
  const isEmpty = !isLoading && entries.length === 0 && page > 0;

  const openFile = (file: GalleryFile) => {
    const target: FileTarget = {
      source: "persisted",
      fileId: file.id,
      filename: file.filename,
      kind: file.kind,
      mediaType: file.media_type,
    };
    if (isViewableKind(file.kind)) onOpenFile(target);
    else void downloadFile(target);
  };

  return (
    <div className="flex-1 overflow-y-auto p-4">
      <p className="sr-only">{total} вложений</p>
      {error ? (
        <div
          role="alert"
          className="mb-4 rounded border border-red-800 bg-red-900/30 px-4 py-2 text-sm text-red-400"
        >
          {error}
        </div>
      ) : null}

      {isEmpty ? (
        <EmptyUploads />
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
            {entries.map((e) =>
              e.kind === "image" ? (
                <ImageCard key={`img-${e.image.id}`} image={e.image} />
              ) : (
                <FileCard
                  key={`file-${e.file.id}`}
                  file={e.file}
                  onOpen={() => openFile(e.file)}
                />
              ),
            )}
            {isLoading
              ? Array.from({ length: 12 }).map((_, i) => (
                  <SkeletonCard key={`skeleton-${i}`} />
                ))
              : null}
          </div>
          {hasMore ? <div ref={sentinelRef} className="h-px w-full" /> : null}
        </>
      )}
    </div>
  );
}

// ── Artifacts mode (opt-in: model-authored files) ───────────────────────────

function ArtifactsView() {
  const [artifacts, setArtifacts] = useState<GalleryArtifact[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0); // last loaded page (0 = none yet)
  const [hasMore, setHasMore] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const loadingRef = useRef(false);

  const loadNext = useCallback(async () => {
    if (loadingRef.current || !hasMore) return;
    loadingRef.current = true;
    setIsLoading(true);
    setError(null);
    const nextPage = page + 1;
    try {
      const res = await apiClient.get<GalleryArtifactsResponse>(
        `/api/v1/files/artifacts?page=${nextPage}&per_page=${PER_PAGE}`,
      );
      setArtifacts((prev) => [...prev, ...res.items]);
      setTotal(res.total);
      setHasMore(res.has_more);
      setPage(nextPage);
    } catch {
      setError("Не удалось загрузить созданные файлы.");
    } finally {
      loadingRef.current = false;
      setIsLoading(false);
    }
  }, [page, hasMore]);

  useEffect(() => {
    void loadNext();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) void loadNext();
      },
      { rootMargin: "400px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [loadNext]);

  const isEmpty = !isLoading && artifacts.length === 0 && page > 0;

  const onDownload = async (artifact: GalleryArtifact) => {
    if (!artifact.conversation_id) return;
    setDownloading(artifact.group_id);
    try {
      await downloadToolArtifact(artifact.conversation_id, artifact.filename);
    } catch {
      setError("Не удалось скачать файл.");
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-4">
      <p className="sr-only">{total} созданных файлов</p>
      {error ? (
        <div
          role="alert"
          className="mb-4 rounded border border-red-800 bg-red-900/30 px-4 py-2 text-sm text-red-400"
        >
          {error}
        </div>
      ) : null}

      {isEmpty ? (
        <EmptyArtifacts />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {artifacts.map((a) => (
              <ArtifactRow
                key={`art-${a.group_id}`}
                artifact={a}
                downloading={downloading === a.group_id}
                onDownload={() => void onDownload(a)}
              />
            ))}
            {isLoading
              ? Array.from({ length: 6 }).map((_, i) => (
                  <SkeletonRow key={`skeleton-${i}`} />
                ))
              : null}
          </div>
          {hasMore ? <div ref={sentinelRef} className="h-px w-full" /> : null}
        </>
      )}
    </div>
  );
}

function ArtifactRow({
  artifact,
  downloading,
  onDownload,
}: {
  artifact: GalleryArtifact;
  downloading: boolean;
  onDownload: () => void;
}) {
  return (
    <div className="group flex items-center gap-3 rounded-lg border border-neutral-800 bg-neutral-900 p-3 transition-colors hover:border-neutral-600">
      <span className="shrink-0 text-emerald-400/80">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
          <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
      </span>
      <div className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-neutral-200" title={artifact.filename}>
          {artifact.filename}
        </span>
        <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wide text-neutral-500">
          {formatFileSize(artifact.size_bytes)} · {formatDateTime(artifact.created_at)}
          {artifact.version_count > 1 ? ` · ${artifact.version_count} версии` : ""}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {artifact.conversation_id ? (
          <Link
            href={`/chat/${artifact.conversation_id}`}
            title="Открыть в чате"
            className="rounded-md px-2 py-1 text-xs text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-200"
          >
            В чат →
          </Link>
        ) : null}
        <button
          type="button"
          onClick={onDownload}
          disabled={downloading || !artifact.conversation_id}
          aria-label={`Скачать ${artifact.filename}`}
          title="Скачать"
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <DownloadIcon />
        </button>
      </div>
    </div>
  );
}

function DownloadIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ImageCard({ image }: { image: GalleryImage }) {
  const src = `data:${image.media_type};base64,${image.thumbnail_base64}`;
  const { archived } = image;
  return (
    <div className="group relative aspect-square overflow-hidden rounded-lg border border-neutral-800 transition-colors hover:border-neutral-600">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt="Изображение"
        className={`h-full w-full object-cover transition-opacity duration-150 ${
          archived ? "opacity-40" : ""
        }`}
      />
      {archived ? (
        <>
          <div className="pointer-events-none absolute inset-0 bg-neutral-950/60" />
          <span className="absolute left-1.5 top-1.5 rounded-md bg-neutral-800/90 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-neutral-400">
            Архив
          </span>
        </>
      ) : null}
      <div className="absolute inset-0 flex flex-col justify-end bg-gradient-to-t from-black/70 to-transparent p-1.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100">
        <span className="text-[10px] text-white/90">
          {formatDateTime(image.created_at)}
        </span>
        {image.conversation_id && !archived ? (
          <Link
            href={`/chat/${image.conversation_id}`}
            className="mt-0.5 text-[11px] font-medium text-white hover:underline"
          >
            Перейти к чату →
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function FileCard({
  file,
  onOpen,
}: {
  file: GalleryFile;
  onOpen: () => void;
}) {
  const { archived } = file;
  return (
    <button
      type="button"
      onClick={onOpen}
      title={`${file.filename} — ${isViewableKind(file.kind) ? "Открыть" : "Скачать"}`}
      className={`group relative flex aspect-square flex-col items-center justify-center gap-2 overflow-hidden rounded-lg border border-neutral-800 bg-neutral-900 p-2 text-center transition-colors hover:border-neutral-600 hover:bg-neutral-800 ${
        archived ? "opacity-50" : ""
      }`}
    >
      <span className="text-neutral-500">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
          <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        </svg>
      </span>
      <span className="w-full min-w-0">
        <span className="block truncate px-1 text-[11px] font-medium text-neutral-200">
          {file.filename}
        </span>
        <span className="mt-0.5 block font-mono text-[9px] uppercase tracking-wide text-neutral-500">
          {file.kind} · {formatFileSize(file.file_size_bytes)}
        </span>
      </span>
      {archived ? (
        <span className="absolute left-1.5 top-1.5 rounded-md bg-neutral-800/90 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-neutral-400">
          Архив
        </span>
      ) : null}
    </button>
  );
}

function SkeletonCard() {
  return (
    <div className="relative aspect-square overflow-hidden rounded-lg bg-neutral-800">
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/10 to-transparent" />
    </div>
  );
}

function SkeletonRow() {
  return (
    <div className="relative h-[58px] overflow-hidden rounded-lg bg-neutral-800">
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/10 to-transparent" />
    </div>
  );
}

function EmptyUploads() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-neutral-400">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6z"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinejoin="round"
        />
        <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      </svg>
      <p className="text-sm">Вы ещё не отправляли файлы или изображения.</p>
    </div>
  );
}

function EmptyArtifacts() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-neutral-400">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9l-6-6z"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinejoin="round"
        />
        <path d="M13 3v6h6" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      </svg>
      <p className="text-sm">ИИ ещё не создавал для вас файлы.</p>
    </div>
  );
}
