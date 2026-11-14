"use client";

import { useState } from "react";
import Image from "next/image";
import { Download, Trash, Check, Loader2, Captions, Sparkles, Clock } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import type { Video, VideoDimensions } from "./types";
import { canRenderThumbnail, detectVideoBadges, formatDuration } from "./video-helpers";
import type { EnhanceJobItem, TranslateJobItem } from "@/app/jobs-context";

interface VideoCardProps {
  video: Video;
  meta?: VideoDimensions;
  enhanceJobs: EnhanceJobItem[];
  translateJobs: TranslateJobItem[];
  actionPendingId: number | null;
  onLoadedMetadata: (videoId: number, width: number, height: number) => void;
  onStartUpscale: (video: Video) => void;
  onStartSubtitle: (video: Video) => void;
  onDelete: (id: number) => void;
}

export function VideoCard({
  video,
  meta,
  enhanceJobs,
  translateJobs,
  actionPendingId,
  onLoadedMetadata,
  onStartUpscale,
  onStartSubtitle,
  onDelete,
}: VideoCardProps) {
  const [downloading, setDownloading] = useState(false);

  const {
    metaHeight,
    isLowRes,
    isEnhanced,
    isSubtitled,
    isForeign,
    isEnhancing,
    isTranslating,
  } = detectVideoBadges(video, meta, enhanceJobs, translateJobs, actionPendingId);

  const isBusy = actionPendingId === video.id;

  async function handleDownloadFile(e: React.MouseEvent) {
    e.stopPropagation();
    try {
      setDownloading(true);
      const res = await fetch(video.url);
      const blob = await res.blob();
      const blobUrl = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      const ext = video.url.split(".").pop()?.split(/[?#]/)[0] || "mp4";
      const safeTitle = (video.title || `video_${video.id}`).replace(/[^a-zA-Z0-9_-]/g, "_");
      a.download = `${safeTitle}.${ext}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(blobUrl);
    } catch {
      window.open(video.url, "_blank");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <Card className="glass-panel overflow-hidden flex flex-col min-h-90 min-w-[320px] rounded-3xl border border-white/10 hover:border-indigo-500/30 shadow-xl hover:shadow-indigo-500/20 transform transition-all hover:-translate-y-2 duration-500 group bg-transparent">
      {canRenderThumbnail(video.thumbnail) && (
        <div className="w-full aspect-video relative overflow-hidden bg-black/40">
          <Image
            src={video.thumbnail}
            alt={video.title || "Video thumbnail"}
            fill
            className="object-cover group-hover:scale-105 transition-transform duration-700 ease-out opacity-90 group-hover:opacity-100"
            sizes="(max-width: 768px) 100vw, (max-width: 1200px) 50vw, 33vw"
            priority={true}
          />
          <div className="absolute inset-0 bg-linear-to-t from-black/80 via-transparent to-transparent" />
        </div>
      )}

      <CardHeader className="pb-2 pt-5 relative z-10">
        <CardTitle
          className="text-lg font-bold wrap-break-word whitespace-pre-line leading-tight truncate text-white drop-shadow-md"
          title={video.title}
        >
          {video.title || "Untitled Video"}
        </CardTitle>
      </CardHeader>

      <CardContent className="flex-1 flex flex-col justify-between z-10 px-6">
        {/* Smart Badges Row */}
        <div className="mb-4 flex flex-wrap items-center gap-1.5">
          {/* 1. Resolution / Upscale Smart Badge */}
          {isEnhanced ? (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[10px] font-semibold tracking-wide uppercase bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 shadow-xs shadow-emerald-500/20">
              <Sparkles className="w-3 h-3 text-emerald-400" />
              Enhanced HD
            </span>
          ) : isEnhancing ? (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-medium bg-indigo-500/20 text-indigo-200 border border-indigo-500/40 animate-pulse">
              <Loader2 className="w-3 h-3 text-indigo-400 animate-spin" />
              Upscaling…
            </span>
          ) : (
            <button
              type="button"
              title={
                isLowRes
                  ? `Low resolution (${metaHeight}p). Click to upscale to HD with Real-ESRGAN`
                  : "Click to upscale video quality with Real-ESRGAN"
              }
              disabled={isBusy || isEnhancing}
              onClick={(e) => {
                e.stopPropagation();
                onStartUpscale(video);
              }}
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-medium transition-all cursor-pointer border ${
                isLowRes
                  ? "bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border-amber-500/40 hover:border-amber-400 shadow-sm shadow-amber-500/20 hover:scale-[1.03]"
                  : "bg-white/5 hover:bg-white/10 text-gray-300 border-white/10 hover:border-indigo-400/50 hover:text-indigo-300"
              }`}
            >
              <Sparkles
                className={`w-3 h-3 ${isLowRes ? "text-amber-400 animate-pulse" : "text-gray-400"}`}
              />
              <span>
                {metaHeight ? `${metaHeight}p` : "Web"} · {isLowRes ? "Upscale to HD" : "Upscale"}
              </span>
            </button>
          )}

          {/* 2. Subtitles / Language Smart Badge */}
          {isSubtitled ? (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[10px] font-semibold tracking-wide uppercase bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 shadow-xs shadow-emerald-500/20">
              <Check className="w-3 h-3 text-emerald-400" />
              Subtitled (EN)
            </span>
          ) : isTranslating ? (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-medium bg-purple-500/20 text-purple-200 border border-purple-500/40 animate-pulse">
              <Loader2 className="w-3 h-3 text-purple-400 animate-spin" />
              Subtitling…
            </span>
          ) : (
            <button
              type="button"
              title={
                isForeign
                  ? "Non-English audio detected. Click to auto-generate English subtitles with Whisper"
                  : "Auto-transcribe & burn English subtitles with Whisper"
              }
              disabled={isBusy || isTranslating}
              onClick={(e) => {
                e.stopPropagation();
                onStartSubtitle(video);
              }}
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-medium transition-all cursor-pointer border ${
                isForeign
                  ? "bg-purple-500/20 hover:bg-purple-500/30 text-purple-300 border-purple-500/40 hover:border-purple-400 shadow-sm shadow-purple-500/20 hover:scale-[1.03]"
                  : "bg-white/5 hover:bg-white/10 text-gray-300 border-white/10 hover:border-purple-400/50 hover:text-purple-300"
              }`}
            >
              <Captions
                className={`w-3 h-3 ${isForeign ? "text-purple-400 animate-pulse" : "text-gray-400"}`}
              />
              <span>{isForeign ? "Foreign · Add Subtitles" : "Add Subtitles"}</span>
            </button>
          )}

          {/* 3. Duration Badge */}
          {video.duration !== undefined && video.duration !== null && (
            <span className="inline-flex items-center gap-1 px-2.5 py-1 bg-black/40 rounded-full text-gray-400 font-mono text-[10px] border border-white/5 ml-auto">
              <Clock className="w-2.5 h-2.5 text-gray-500" />
              {formatDuration(video.duration)}
            </span>
          )}
        </div>

        {/* Video Player */}
        <div className="flex justify-between items-center rounded-xl overflow-hidden shadow-inner bg-black/40 relative group-hover:shadow-indigo-500/20 transition-all aspect-video">
          <video
            className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity duration-300"
            controls
            preload="metadata"
            onLoadedMetadata={(e) => {
              const el = e.currentTarget;
              if (el.videoHeight) {
                onLoadedMetadata(video.id, el.videoWidth, el.videoHeight);
              }
            }}
          >
            <source src={video.url} type="video/mp4" />
            <source src={video.url} type="video/webm" />
            Your browser does not support the video tag.
          </video>
        </div>
      </CardContent>

      <CardFooter className="flex justify-between items-center gap-2 pt-0 pb-5 px-6 z-10">
        <div className="text-[11px] text-gray-400">
          {isEnhanced && isSubtitled ? (
            <span className="text-emerald-400/90 font-medium">Fully Polished</span>
          ) : isEnhanced ? (
            <span className="text-indigo-300/80">Upscaled Video</span>
          ) : isSubtitled ? (
            <span className="text-purple-300/80">Subtitled Video</span>
          ) : (
            <span className="text-gray-500">Original Web Download</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            variant="outline"
            size="icon"
            title="Download Video File"
            disabled={downloading}
            onClick={handleDownloadFile}
            className="hover:border-indigo-500/50 hover:bg-indigo-500/10 hover:text-indigo-400 text-gray-400 transition-all cursor-pointer rounded-xl h-9 w-9 bg-white/5 border-white/10"
          >
            {downloading ? <Loader2 className="w-4 h-4 animate-spin text-indigo-400" /> : <Download className="w-4 h-4" />}
          </Button>
          <Button
            variant="outline"
            size="icon"
            onClick={() => onDelete(video.id)}
            className="hover:border-red-500/50 hover:bg-red-500/10 hover:text-red-400 text-gray-400 transition-all cursor-pointer rounded-xl h-9 w-9 bg-white/5 border-white/10"
          >
            <Trash className="w-4 h-4" />
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}
