import type { DownloadJob, EnhanceJobData, EnhanceJobItem, TranslateJobData, TranslateJobItem } from "@/app/jobs-context";
import type { Video, VideoDimensions } from "./types";

export const PAGE_SIZE = 20;
export const QUEUE_TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export const STATUS_STYLES: Record<DownloadJob["status"], string> = {
  extracting: "bg-yellow-500/20 text-yellow-400",
  adding:     "bg-indigo-500/20 text-indigo-400",
  queued:     "bg-orange-500/20 text-orange-400",
  processing: "bg-blue-500/20 text-blue-400",
  done:       "bg-green-500/20 text-green-400",
  failed:     "bg-red-500/20 text-red-400",
  cancelled:  "bg-gray-500/20 text-gray-400",
};

export function formatDuration(seconds?: number): string {
  if (!seconds || isNaN(seconds)) return "Unknown";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function canRenderThumbnail(thumbnail?: string): thumbnail is string {
  return Boolean(thumbnail && thumbnail.startsWith("/") && !thumbnail.startsWith("//"));
}

export function phaseMessage(phase?: string): string {
  switch (phase) {
    case "fast_pass": return "Analyzing video source…";
    case "heavy_pass_waiting": return "Loading video player…";
    case "heavy_pass_recording": return "Recording video stream…";
    default: return "Processing video…";
  }
}

export function translatePhaseMessage(data: TranslateJobData): string {
  if (data.status === "queued") return "Waiting in queue…";
  if (data.status === "done") return "Subtitled video ready!";
  if (data.status === "failed") return data.error || "Subtitling failed";
  switch (data.phase) {
    case "extracting_audio": return "Extracting audio…";
    case "transcribing": return "Transcribing with Whisper…";
    case "translating": return data.total_chunks > 0 ? `Translating (${data.chunk_index}/${data.total_chunks})…` : "Translating…";
    case "burning": return `Burning subtitles… ${data.overall_progress}%`;
    default: return "Processing subtitles…";
  }
}

export function enhancePhaseMessage(data: EnhanceJobData): string {
  if (data.status === "queued") return "Queued for upscale…";
  if (data.status === "done") return "Upscaled video ready!";
  if (data.status === "failed") return data.error || "Upscale failed";
  switch (data.phase) {
    case "splitting": return "Splitting video into chunks…";
    case "assembling": return "Assembling final video…";
    default:
      if (data.phase.startsWith("enhancing")) {
        const chunk = data.phase.split(" ")[1] ?? "1/1";
        return `Upscaling chunk ${chunk} — ${data.progress}%`;
      }
      return `Upscaling video… ${data.progress}%`;
  }
}

export function queueResultVideo(result: unknown): Video | undefined {
  if (!result || typeof result !== "object" || !("video" in result)) return undefined;
  const video = (result as { video?: Partial<Video> }).video;
  if (
    !video ||
    typeof video.id !== "number" ||
    typeof video.url !== "string" ||
    typeof video.category !== "string" ||
    typeof video.created_at !== "string"
  ) {
    return undefined;
  }
  return {
    id: video.id,
    url: video.url,
    category: video.category,
    title: video.title,
    created_at: video.created_at,
    duration: video.duration,
    thumbnail: video.thumbnail,
  };
}

export interface VideoBadgeState {
  metaHeight?: number;
  isLowRes: boolean;
  isEnhanced: boolean;
  isSubtitled: boolean;
  isForeign: boolean;
  isEnhancing: boolean;
  isTranslating: boolean;
}

export function detectVideoBadges(
  video: Video,
  meta?: VideoDimensions,
  enhanceJobs: EnhanceJobItem[] = [],
  translateJobs: TranslateJobItem[] = [],
  actionPendingId: number | null = null,
): VideoBadgeState {
  const metaHeight = meta?.height;
  const isLowRes = metaHeight ? metaHeight < 720 : false;
  const isEnhanced = video.category === "enhanced" || Boolean(video.title?.includes("(Upscaled)"));
  const isSubtitled = video.category === "translated" || Boolean(video.title?.includes("(Subtitled)"));
  const isForeign = /[^\x00-\x7F]/.test(video.title || "");
  const titleMatch = video.title || `video_${video.id}`;

  const isEnhancing =
    actionPendingId === video.id ||
    enhanceJobs.some(
      (j) =>
        (j.filename.includes(titleMatch) || j.filename.includes(`video_${video.id}`)) &&
        j.data.status !== "done" &&
        j.data.status !== "failed"
    );

  const isTranslating =
    actionPendingId === video.id ||
    translateJobs.some(
      (j) =>
        (j.filename.includes(titleMatch) || j.filename.includes(`video_${video.id}`)) &&
        j.data.status !== "done" &&
        j.data.status !== "failed"
    );

  return {
    metaHeight,
    isLowRes,
    isEnhanced,
    isSubtitled,
    isForeign,
    isEnhancing,
    isTranslating,
  };
}
