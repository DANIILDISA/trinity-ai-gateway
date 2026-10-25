import { artifactExtension, type Artifact } from "./artifacts";

// Save an artifact's content as a file (used by the in-stream card's download
// button — light path, no server round-trip). Names the file from the artifact
// title + its kind/language extension.
export function downloadArtifact(artifact: Artifact) {
  const blob = new Blob([artifact.content], {
    type: "text/plain;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${artifact.title}${artifactExtension(artifact)}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
