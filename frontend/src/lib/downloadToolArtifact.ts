import { apiClient } from "./api";
import { artifactContentToBlob } from "./binaryBlob";

// The artifact endpoint returns a model-authored file's content as JSON (mirrors
// GET /files/{id}). Server-side content, so it works after a reload without
// keeping the file text in memory. `version` is the returned row's version and
// `version_count` is how many versions exist for that file — the canvas shows a
// <n/m> switcher when version_count > 1 (tr-qil).
export interface ArtifactPayload {
  filename: string;
  media_type: string;
  content: string;
  version: number;
  version_count: number;
}

// Fetch a model-authored artifact's content from the per-conversation endpoint
// (tr-cy0.2.13). Keyed by (conversation, filename); defaults to the LATEST
// version, or pass `version` to fetch a specific one (tr-qil). Shared by the
// «Скачать» action, the canvas-open path (tr-lqn) and the version switcher.
export async function fetchToolArtifact(
  conversationId: string,
  filename: string,
  version?: number,
): Promise<ArtifactPayload> {
  const versionQuery = version === undefined ? "" : `&version=${version}`;
  return apiClient.get<ArtifactPayload>(
    `/api/v1/chat/conversations/${conversationId}/artifact?filename=${encodeURIComponent(
      filename,
    )}${versionQuery}`,
  );
}

// Trigger a browser "save as" for a Blob under `filename` (shared by the artifact
// download paths + the run_python generated-file chip, tr-an0g.5).
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// Fetch a model-authored artifact and save it under its real filename with the
// server-reported media type (tr-cy0.2.13). Errors propagate so the card can
// surface a failed state. Keyed by (conversation, filename); the endpoint always
// returns the latest version.
export async function downloadToolArtifact(
  conversationId: string,
  filename: string,
): Promise<void> {
  const data = await fetchToolArtifact(conversationId, filename);
  // Binary artifacts (PNG, xlsx, parquet…) store base64 — decode to real bytes;
  // text artifacts keep their UTF-8 content verbatim (tr-an0g).
  saveBlob(artifactContentToBlob(data.content, data.media_type), data.filename);
}
