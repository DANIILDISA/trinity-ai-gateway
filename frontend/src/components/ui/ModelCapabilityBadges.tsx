import type { ReactNode } from "react";

import { PaperclipIcon } from "@/components/ui/PaperclipIcon";

export interface ModelCapabilityBadgesProps {
  supportsImageInput: boolean;
  supportsFileInput: boolean;
  supportsTools: boolean;
  supportsReasoning: boolean;
  supportsWebSearch: boolean;
}

interface Badge {
  enabled: boolean;
  // Either an emoji string or an icon component (e.g. the shared paperclip).
  icon: ReactNode;
  label: string;
  tooltip?: { minW: string; content: ReactNode };
}

export function ModelCapabilityBadges({
  supportsImageInput,
  supportsFileInput,
  supportsTools,
  supportsReasoning,
  supportsWebSearch,
}: ModelCapabilityBadgesProps) {
  const badges: Badge[] = [
    {
      enabled: supportsImageInput,
      icon: <PaperclipIcon />,
      label: "Анализ фото",
      tooltip: {
        minW: "min-w-[240px]",
        content: (
          <>
            <span className="font-semibold">
              Поддерживаемые форматы изображений
            </span>
            <span>• JPEG / JPG</span>
            <span>• PNG</span>
            <span>• GIF (статичный и анимированный)</span>
            <span>• WebP</span>
            <span className="mt-1 text-zinc-400">
              Изображение передаётся как часть сообщения
            </span>
          </>
        ),
      },
    },
    {
      enabled: supportsFileInput,
      icon: <PaperclipIcon />,
      label: "Файлы",
      tooltip: {
        minW: "min-w-[220px]",
        content: (
          <>
            <span className="font-semibold">Поддерживаемые форматы</span>
            <span>• PDF (.pdf) — нативная обработка или извлечение текста</span>
            <span>• Текст (.txt, .md) — через file-parser</span>
            <span className="mt-1 text-zinc-400">
              Файл передаётся как часть сообщения
            </span>
          </>
        ),
      },
    },
    {
      enabled: supportsTools,
      icon: "🔧",
      label: "Инструменты",
      tooltip: {
        minW: "min-w-[240px]",
        content: (
          <>
            <span className="font-semibold">Function Calling</span>
            <span>
              Позволяет модели вызывать внешние функции и инструменты во время
              генерации ответа.
            </span>
            <span className="mt-1 text-amber-500">
              ⚠ Функция находится в разработке
            </span>
          </>
        ),
      },
    },
    {
      enabled: supportsReasoning,
      icon: null,
      label: "Thinking",
      tooltip: {
        minW: "min-w-[260px]",
        content: (
          <>
            <span className="font-semibold">Расширенное мышление</span>
            <span>
              Модель думает перед ответом — строит цепочку рассуждений во
              внутренних токенах. Даёт более точные ответы на сложные задачи, но
              стоит дороже и отвечает медленнее.
            </span>
            <span className="mt-1 text-zinc-400">
              Внутренние токены тарифицируются отдельно
            </span>
          </>
        ),
      },
    },
    { enabled: supportsWebSearch, icon: "🌐", label: "Веб-поиск" },
  ];

  const active = badges.filter((b) => b.enabled);
  if (active.length === 0) return null;

  const pillClass =
    "inline-flex items-center gap-1 rounded bg-zinc-700 px-1.5 py-0.5 text-xs text-zinc-300";

  return (
    <span className="inline-flex flex-wrap gap-1">
      {active.map((b) =>
        b.tooltip ? (
          <span key={b.label} className="group relative inline-block">
            <span className={`${pillClass} cursor-default`}>
              {b.icon ? (
                <span className="inline-flex shrink-0 items-center">{b.icon}</span>
              ) : null}
              {b.label}
            </span>
            <span
              className={`pointer-events-none absolute bottom-full left-0 z-50 mb-2 hidden ${b.tooltip.minW} flex-col gap-1 rounded-md border border-zinc-600 bg-zinc-800 p-2 text-xs text-zinc-100 shadow-lg group-hover:flex`}
            >
              {b.tooltip.content}
            </span>
          </span>
        ) : (
          <span key={b.label} className={pillClass}>
            {b.icon ? (
              <span className="inline-flex shrink-0 items-center">{b.icon}</span>
            ) : null}
            {b.label}
          </span>
        ),
      )}
    </span>
  );
}

export default ModelCapabilityBadges;
