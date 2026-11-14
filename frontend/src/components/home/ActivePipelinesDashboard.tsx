"use client";

import { Check, X, Loader2, Download, Captions, Sparkles } from "lucide-react";
import type { DownloadJob, EnhanceJobItem, TranslateJobItem } from "@/app/jobs-context";
import { triggerFileDownload } from "@/app/job-utils";
import { RecordingTimer, RecordingProgress } from "./RecordingProgress";
import { translatePhaseMessage, enhancePhaseMessage } from "./video-helpers";

interface ActivePipelinesDashboardProps {
  downloads: DownloadJob[];
  translateJobs: TranslateJobItem[];
  enhanceJobs: EnhanceJobItem[];
  onCancelDownload: (job: DownloadJob) => void;
  onCancelTranslate: (localId: string) => void;
  onCancelEnhance: (localId: string) => void;
  onDismissTranslate: (localId: string) => void;
  onDismissEnhance: (localId: string) => void;
}

export function ActivePipelinesDashboard({
  downloads,
  translateJobs,
  enhanceJobs,
  onCancelDownload,
  onCancelTranslate,
  onCancelEnhance,
  onDismissTranslate,
  onDismissEnhance,
}: ActivePipelinesDashboardProps) {
  const activeCount =
    downloads.filter((d) => d.status !== "done" && d.status !== "failed" && d.status !== "cancelled").length +
    translateJobs.filter((tj) => tj.data.status !== "done" && tj.data.status !== "failed").length +
    enhanceJobs.filter((ej) => ej.data.status !== "done" && ej.data.status !== "failed").length;

  if (downloads.length === 0 && translateJobs.length === 0 && enhanceJobs.length === 0) {
    return null;
  }

  return (
    <div className="mb-10">
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-base font-semibold text-white">Active Pipelines & Downloads</h2>
        {activeCount > 0 && (
          <span className="flex items-center gap-1.5 text-xs text-indigo-300 bg-indigo-500/10 border border-indigo-500/20 px-2.5 py-1 rounded-full">
            <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
            {activeCount} active
          </span>
        )}
      </div>

      <div className="space-y-2.5">
        {/* Download items */}
        {downloads.map((dl) => (
          <div
            key={dl.localId}
            className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
              dl.status === "done"
                ? "border-green-500/25"
                : dl.status === "failed"
                ? "border-red-500/40 bg-red-500/5"
                : dl.phase === "heavy_pass_recording"
                ? "border-red-500/25"
                : "border-indigo-500/20"
            }`}
          >
            <div className="shrink-0">
              {dl.status === "done" ? (
                <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                  <Check className="w-4 h-4 text-green-400" />
                </div>
              ) : dl.status === "failed" ? (
                <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                  <X className="w-4 h-4 text-red-400" />
                </div>
              ) : dl.phase === "heavy_pass_recording" ? (
                <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                  <span className="w-2.5 h-2.5 rounded-full bg-red-500 animate-ping" />
                </div>
              ) : (
                <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                  <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                </div>
              )}
            </div>

            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white truncate">
                {dl.title || dl.url}
              </p>
              <div className="flex items-center gap-2 mt-0.5">
                <p
                  className={`text-xs ${
                    dl.status === "done"
                      ? "text-green-400"
                      : dl.status === "failed"
                      ? "text-red-400"
                      : dl.phase === "heavy_pass_recording"
                      ? "text-red-400 font-mono"
                      : "text-gray-400"
                  }`}
                >
                  {dl.phase === "heavy_pass_recording" && dl.recordingStartedAt ? (
                    <span className="flex items-center gap-1.5">
                      <span>Recording stream…</span>
                      <RecordingTimer startedAt={dl.recordingStartedAt} />
                    </span>
                  ) : (
                    dl.message
                  )}
                </p>
                {dl.status === "failed" && dl.errorDetail && (
                  <span
                    className="text-[10px] text-red-400/80 cursor-help underline decoration-dotted"
                    title={dl.errorDetail}
                  >
                    Details
                  </span>
                )}
              </div>

              {dl.phase === "heavy_pass_recording" &&
                dl.recordingStartedAt &&
                dl.recordingDuration && (
                  <div className="w-full h-1 rounded-full overflow-hidden mt-2 bg-white/5">
                    <RecordingProgress
                      startedAt={dl.recordingStartedAt}
                      durationSeconds={dl.recordingDuration}
                    />
                  </div>
                )}

              {dl.phase === "fast_pass" && dl.downloadProgress != null && (
                <div className="w-full h-1 rounded-full overflow-hidden mt-2 bg-white/5">
                  <div
                    className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                    style={{ width: `${Math.min(100, dl.downloadProgress)}%` }}
                  />
                </div>
              )}
            </div>

            {dl.status !== "done" && dl.status !== "failed" && dl.status !== "cancelled" && (
              <button
                type="button"
                onClick={() => onCancelDownload(dl)}
                className="shrink-0 p-1.5 rounded-lg text-gray-500 hover:text-red-400 hover:bg-red-500/10 transition-colors cursor-pointer"
                title="Cancel download"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        ))}

        {/* Translation items */}
        {translateJobs.map((tj) => (
          <div
            key={tj.localId}
            className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
              tj.data.status === "done"
                ? "border-green-500/25"
                : tj.data.status === "failed"
                ? "border-red-500/40 bg-red-500/5"
                : "border-purple-500/30 bg-purple-500/5"
            }`}
          >
            <div className="shrink-0">
              {tj.data.status === "done" ? (
                <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                  <Check className="w-4 h-4 text-green-400" />
                </div>
              ) : tj.data.status === "failed" ? (
                <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                  <X className="w-4 h-4 text-red-400" />
                </div>
              ) : (
                <div className="w-8 h-8 rounded-full bg-purple-500/15 flex items-center justify-center">
                  <Loader2 className="w-4 h-4 text-purple-400 animate-spin" />
                </div>
              )}
            </div>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/30 flex items-center gap-1">
                  <Captions className="w-3 h-3" /> Auto-Subtitle
                </span>
                <p className="text-sm font-medium text-white truncate">{tj.filename}</p>
              </div>
              <p
                className={`text-xs mt-1 ${
                  tj.data.status === "done"
                    ? "text-green-400"
                    : tj.data.status === "failed"
                    ? "text-red-400"
                    : "text-purple-300"
                }`}
              >
                {translatePhaseMessage(tj.data)}
              </p>
              {tj.data.status !== "done" && tj.data.status !== "failed" && (
                <div className="w-full h-1 rounded-full overflow-hidden mt-2 bg-white/5">
                  <div
                    className="h-full bg-purple-500 rounded-full transition-all duration-500"
                    style={{ width: `${Math.max(8, tj.data.overall_progress || 0)}%` }}
                  />
                </div>
              )}
            </div>

            {tj.data.status === "done" && tj.data.result_url && (
              <button
                title="Download Subtitled Video"
                onClick={() =>
                  triggerFileDownload(
                    tj.data.result_url!,
                    `${tj.filename.replace(/\.[^/.]+$/, "")}_subtitled.mp4`
                  )
                }
                className="shrink-0 p-2 rounded-xl bg-green-500/10 text-green-400 hover:bg-green-500 hover:text-white transition-all border border-green-500/20 cursor-pointer"
              >
                <Download className="w-4 h-4" />
              </button>
            )}

            {tj.data.status !== "done" && tj.data.status !== "failed" && (
              <button
                title="Cancel"
                onClick={() => onCancelTranslate(tj.localId)}
                className="shrink-0 text-gray-500 hover:text-red-400 transition-colors ml-1 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            )}

            {(tj.data.status === "done" || tj.data.status === "failed") && (
              <button
                title="Dismiss"
                onClick={() => onDismissTranslate(tj.localId)}
                className="shrink-0 text-gray-500 hover:text-white transition-colors ml-1 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        ))}

        {/* Enhancement items */}
        {enhanceJobs.map((ej) => (
          <div
            key={ej.localId}
            className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
              ej.data.status === "done"
                ? "border-green-500/25"
                : ej.data.status === "failed"
                ? "border-red-500/40 bg-red-500/5"
                : "border-indigo-500/30 bg-indigo-500/5"
            }`}
          >
            <div className="shrink-0">
              {ej.data.status === "done" ? (
                <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                  <Check className="w-4 h-4 text-green-400" />
                </div>
              ) : ej.data.status === "failed" ? (
                <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                  <X className="w-4 h-4 text-red-400" />
                </div>
              ) : (
                <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                  <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                </div>
              )}
            </div>

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 flex items-center gap-1">
                  <Sparkles className="w-3 h-3" /> Upscale
                </span>
                <p className="text-sm font-medium text-white truncate">{ej.filename}</p>
              </div>
              <p
                className={`text-xs mt-1 ${
                  ej.data.status === "done"
                    ? "text-green-400"
                    : ej.data.status === "failed"
                    ? "text-red-400"
                    : "text-indigo-300"
                }`}
              >
                {enhancePhaseMessage(ej.data)}
              </p>
              {ej.data.status !== "done" && ej.data.status !== "failed" && (
                <div className="w-full h-1 rounded-full overflow-hidden mt-2 bg-white/5">
                  <div
                    className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                    style={{ width: `${Math.max(8, ej.data.progress || 0)}%` }}
                  />
                </div>
              )}
            </div>

            {ej.data.status === "done" && ej.data.result_url && (
              <button
                title="Download Upscaled Video"
                onClick={() =>
                  triggerFileDownload(
                    ej.data.result_url!,
                    `${ej.filename.replace(/\.[^/.]+$/, "")}_upscaled.mp4`
                  )
                }
                className="shrink-0 p-2 rounded-xl bg-green-500/10 text-green-400 hover:bg-green-500 hover:text-white transition-all border border-green-500/20 cursor-pointer"
              >
                <Download className="w-4 h-4" />
              </button>
            )}

            {ej.data.status !== "done" && ej.data.status !== "failed" && (
              <button
                title="Cancel"
                onClick={() => onCancelEnhance(ej.localId)}
                className="shrink-0 text-gray-500 hover:text-red-400 transition-colors ml-1 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            )}

            {(ej.data.status === "done" || ej.data.status === "failed") && (
              <button
                title="Dismiss"
                onClick={() => onDismissEnhance(ej.localId)}
                className="shrink-0 text-gray-500 hover:text-white transition-colors ml-1 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
