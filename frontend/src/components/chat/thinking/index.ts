// Public entry point for the thinking timeline. Importing this module registers
// the built-in segment renderers as a side effect, so consumers must import
// ThinkingProcess / buildThinkingProcess from HERE (never the individual files)
// — that guarantees the register() calls below have run before anything renders.
//
// INVARIANT: do not mark this file side-effect-free. The project's
// package.json has no "sideEffects" field, so these top-level register() calls
// are preserved by the bundler. If a "sideEffects" allowlist is ever added to
// package.json, this file's path MUST be included or the registry will be empty
// in production builds.
import { register } from "./registry";
import { ReasoningSegment } from "./segments/ReasoningSegment";
import { ToolStepSegment } from "./segments/ToolStepSegment";

register("reasoning", ReasoningSegment);
register("tool_call", ToolStepSegment);

export { ThinkingProcess } from "./ThinkingProcess";
export { buildThinkingProcess } from "./buildThinkingProcess";
