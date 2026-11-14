"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Download, Loader2, Trash2 } from "lucide-react";
import { useAuth } from "./auth-context";
import { useJobs } from "./jobs-context";
import type { DownloadJob } from "./jobs-context";
import { queuedDownloadRows, downloadBlob, videoDownloadName } from "./job-utils";
import {
  listVideos,
  addVideo,
  deleteVideo,
  extractVideo,
  cancelJob,
  downloadVideo,
  startEnhanceForVideo,
  startTranslateForVideo,
  cancelEnhanceJob,
  cancelTranslateJob,
  getQueueStatus,
} from "./api";
import { Button } from "@/components/ui/button";
import Navbar from "@/components/Navbar";

import type { Video, VideoDimensions } from "@/components/home/types";
import {
  PAGE_SIZE,
  QUEUE_TERMINAL_STATUSES,
  queueResultVideo,
  phaseMessage,
} from "@/components/home/video-helpers";
import { AddVideoForm } from "@/components/home/AddVideoForm";
import { ActivePipelinesDashboard } from "@/components/home/ActivePipelinesDashboard";
import { VideoCard } from "@/components/home/VideoCard";
import { DeleteDialogs } from "@/components/home/DeleteDialogs";

export default function HomePage() {
  const { token, loading } = useAuth();
  const router = useRouter();
  const [videos, setVideos] = useState<Video[]>([]);
  const [videoMeta, setVideoMeta] = useState<Record<number, VideoDimensions>>({});
  const [url, setUrl] = useState("");
  const [actionPendingId, setActionPendingId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(true);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  const [showDeleteAllDialog, setShowDeleteAllDialog] = useState(false);
  const [downloadingAll, setDownloadingAll] = useState(false);
  const loaderRef = useRef<HTMLDivElement | null>(null);
  const [fetching, setFetching] = useState(false);

  const {
    downloads,
    setDownloads,
    enhanceJobs,
    setEnhanceJobs,
    enhancePollRefs,
    translateJobs,
    setTranslateJobs,
    translatePollRefs,
  } = useJobs();

  const downloadsRef = useRef<DownloadJob[]>([]);
  const pollFailuresRef = useRef<Map<string, number>>(new Map());

  // Keep ref in sync so polling interval always sees latest state
  useEffect(() => {
    downloadsRef.current = downloads;
  }, [downloads]);

  const fetchMoreVideos = useCallback(
    async (reset = false) => {
      if (!token) return;
      setFetching(true);
      try {
        const skip = reset ? 0 : videos.length;
        const newVideos = await listVideos(token, undefined, skip, PAGE_SIZE);
        if (newVideos.length < PAGE_SIZE) setHasMore(false);
        setVideos((prev) => {
          if (reset) return newVideos;
          const existingIds = new Set(prev.map((v) => v.id));
          return [...prev, ...newVideos.filter((v: Video) => !existingIds.has(v.id))];
        });
      } catch {
        setError("Failed to load videos");
      } finally {
        setFetching(false);
      }
    },
    [token, videos.length]
  );

  const startEnhancePolling = useCallback(
    (localId: string, jobId: string) => {
      if (enhancePollRefs.current.has(localId)) return;
      const intervalId = setInterval(async () => {
        if (!token) return;
        try {
          const data = await getQueueStatus(token, jobId);
          setEnhanceJobs((prev) =>
            prev.map((job) => (job.localId === localId ? { ...job, data } : job))
          );
          if (data.status === "done" || data.status === "failed" || data.status === "cancelled") {
            const iv = enhancePollRefs.current.get(localId);
            if (iv) clearInterval(iv);
            enhancePollRefs.current.delete(localId);
            if (data.status === "done") {
              fetchMoreVideos(true);
            }
          }
        } catch (err: unknown) {
          const status = (err as { response?: { status?: number } })?.response?.status;
          if (status === 404) {
            const iv = enhancePollRefs.current.get(localId);
            if (iv) clearInterval(iv);
            enhancePollRefs.current.delete(localId);
            setEnhanceJobs((prev) =>
              prev.map((job) =>
                job.localId === localId
                  ? {
                      ...job,
                      data: {
                        ...job.data,
                        status: "failed",
                        error: "Job expired or not found on server",
                        phase: "failed",
                      },
                    }
                  : job
              )
            );
          }
        }
      }, 1000);
      enhancePollRefs.current.set(localId, intervalId);
    },
    [enhancePollRefs, fetchMoreVideos, setEnhanceJobs, token]
  );

  const cancelLocalEnhanceJob = useCallback(
    (localId: string, cancelFn: (token: string, jobId: string) => Promise<void>) => {
      const iv = enhancePollRefs.current.get(localId);
      if (iv) clearInterval(iv);
      enhancePollRefs.current.delete(localId);
      const job = enhanceJobs.find((j) => j.localId === localId);
      if (job?.jobId && token) cancelFn(token, job.jobId).catch(() => {});
      setEnhanceJobs((prev) => prev.filter((j) => j.localId !== localId));
    },
    [enhanceJobs, enhancePollRefs, setEnhanceJobs, token]
  );

  const removeEnhanceJob = useCallback(
    (localId: string) => {
      setEnhanceJobs((prev) => prev.filter((j) => j.localId !== localId));
    },
    [setEnhanceJobs]
  );

  const startTranslatePolling = useCallback(
    (localId: string, jobId: string) => {
      if (translatePollRefs.current.has(localId)) return;
      const intervalId = setInterval(async () => {
        if (!token) return;
        try {
          const data = await getQueueStatus(token, jobId);
          setTranslateJobs((prev) =>
            prev.map((job) => (job.localId === localId ? { ...job, data } : job))
          );
          if (data.status === "done" || data.status === "failed" || data.status === "cancelled") {
            const iv = translatePollRefs.current.get(localId);
            if (iv) clearInterval(iv);
            translatePollRefs.current.delete(localId);
            if (data.status === "done") {
              fetchMoreVideos(true);
            }
          }
        } catch (err: unknown) {
          const status = (err as { response?: { status?: number } })?.response?.status;
          if (status === 404) {
            const iv = translatePollRefs.current.get(localId);
            if (iv) clearInterval(iv);
            translatePollRefs.current.delete(localId);
            setTranslateJobs((prev) =>
              prev.map((job) =>
                job.localId === localId
                  ? {
                      ...job,
                      data: {
                        ...job.data,
                        status: "failed",
                        error: "Job expired or not found on server",
                        phase: "failed",
                      },
                    }
                  : job
              )
            );
          }
        }
      }, 1000);
      translatePollRefs.current.set(localId, intervalId);
    },
    [fetchMoreVideos, setTranslateJobs, token, translatePollRefs]
  );

  const cancelLocalTranslateJob = useCallback(
    (localId: string, cancelFn: (token: string, jobId: string) => Promise<void>) => {
      const iv = translatePollRefs.current.get(localId);
      if (iv) clearInterval(iv);
      translatePollRefs.current.delete(localId);
      const job = translateJobs.find((j) => j.localId === localId);
      if (job?.jobId && token) cancelFn(token, job.jobId).catch(() => {});
      setTranslateJobs((prev) => prev.filter((j) => j.localId !== localId));
    },
    [setTranslateJobs, token, translateJobs, translatePollRefs]
  );

  const removeTranslateJob = useCallback(
    (localId: string) => {
      setTranslateJobs((prev) => prev.filter((j) => j.localId !== localId));
    },
    [setTranslateJobs]
  );

  const handleStartSubtitle = useCallback(
    async (targetVideo: Video) => {
      if (!token) return;
      setActionPendingId(targetVideo.id);
      try {
        const res = await startTranslateForVideo(token, targetVideo.id);
        const localId = `translate-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const filename = targetVideo.title || `video_${targetVideo.id}.mp4`;
        setTranslateJobs((prev) => [
          ...prev,
          {
            localId,
            filename,
            jobId: res.job_id,
            uploadProgress: 100,
            data: {
              job_id: res.job_id,
              filename,
              status: "queued",
              phase: "extracting_audio",
              chunk_index: 0,
              total_chunks: 0,
              overall_progress: 0,
            },
          },
        ]);
        startTranslatePolling(localId, res.job_id);
      } catch (e: unknown) {
        const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
        setError(msg || (e instanceof Error ? e.message : "Failed to queue translation"));
      } finally {
        setActionPendingId(null);
      }
    },
    [token, setTranslateJobs, startTranslatePolling]
  );

  const handleStartUpscale = useCallback(
    async (targetVideo: Video) => {
      if (!token) return;
      setActionPendingId(targetVideo.id);
      try {
        const res = await startEnhanceForVideo(token, targetVideo.id);
        const localId = `enhance-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const filename = targetVideo.title || `video_${targetVideo.id}.mp4`;
        setEnhanceJobs((prev) => [
          ...prev,
          {
            localId,
            filename,
            jobId: res.job_id,
            uploadProgress: 100,
            data: {
              job_id: res.job_id,
              status: "queued",
              phase: "splitting",
              progress: 0,
            },
          },
        ]);
        startEnhancePolling(localId, res.job_id);
      } catch (e: unknown) {
        const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
        setError(msg || (e instanceof Error ? e.message : "Failed to queue upscale"));
      } finally {
        setActionPendingId(null);
      }
    },
    [token, setEnhanceJobs, startEnhancePolling]
  );

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const hasActiveQueueJobs = downloads.some(
    (d) => d.jobId && (d.status === "queued" || d.status === "processing")
  );

  useEffect(() => {
    if (!token || !hasActiveQueueJobs) return;

    const pollActiveJobs = async () => {
      const active = downloadsRef.current.filter(
        (d) => d.jobId && (d.status === "queued" || d.status === "processing")
      );
      if (active.length === 0) return;

      await Promise.all(
        active.map(async (job) => {
          try {
            const data = await getQueueStatus(token, job.jobId!);
            pollFailuresRef.current.delete(job.localId);

            if (QUEUE_TERMINAL_STATUSES.has(data.status)) {
              const isDone = data.status === "done";
              setDownloads((prev) =>
                prev.map((d) =>
                  d.localId === job.localId
                    ? {
                        ...d,
                        status: data.status,
                        message:
                          isDone
                            ? "Video ready!"
                            : data.status === "cancelled"
                            ? "Cancelled"
                            : "Recording failed",
                        errorDetail: data.status === "failed" ? data.error : undefined,
                      }
                    : d
                )
              );
              if (isDone) {
                const completedVideo = queueResultVideo(data.result);
                if (completedVideo) {
                  setVideos((prev) => {
                    const existingIndex = prev.findIndex((video) => video.id === completedVideo.id);
                    if (existingIndex >= 0) {
                      return prev.map((video, index) =>
                        index === existingIndex ? completedVideo : video
                      );
                    }
                    return [completedVideo, ...prev];
                  });
                } else {
                  fetchMoreVideos(true);
                }
                setTimeout(() => {
                  setDownloads((prev) => prev.filter((d) => d.localId !== job.localId));
                }, 5000);
              }
            } else {
              const phase = data.phase as string | undefined;
              const recordingStartedAt =
                data.recording_started_at != null
                  ? data.recording_started_at * 1000
                  : job.recordingStartedAt;
              const downloadProgress =
                typeof data.download_progress === "number"
                  ? data.download_progress
                  : job.downloadProgress;
              const recordingDuration =
                typeof data.recording_duration === "number"
                  ? data.recording_duration
                  : job.recordingDuration;
              const msg =
                data.status === "processing"
                  ? phaseMessage(phase)
                  : `Queued for processing${
                      data.queue_position != null ? ` · Position ${data.queue_position}` : ""
                    }`;
              setDownloads((prev) =>
                prev.map((d) =>
                  d.localId === job.localId
                    ? {
                        ...d,
                        status: data.status,
                        message: msg,
                        queuePosition: data.queue_position,
                        phase,
                        recordingStartedAt,
                        downloadProgress,
                        recordingDuration,
                      }
                    : d
                )
              );
            }
          } catch (err: unknown) {
            const status = (err as { response?: { status?: number } })?.response?.status;
            if (status === 404) {
              pollFailuresRef.current.delete(job.localId);
              setDownloads((prev) =>
                prev.map((d) =>
                  d.localId === job.localId
                    ? {
                        ...d,
                        status: "failed",
                        message: "Job expired or not found",
                        errorDetail: "The download job is no longer available on the server.",
                      }
                    : d
                )
              );
              setTimeout(() => {
                setDownloads((prev) => prev.filter((d) => d.localId !== job.localId));
              }, 4000);
            } else {
              const fails = (pollFailuresRef.current.get(job.localId) || 0) + 1;
              pollFailuresRef.current.set(job.localId, fails);
              console.warn(
                `Polling attempt ${fails} for job "${job.jobId}":`,
                (err as Error)?.message || err
              );

              if (fails >= 15) {
                pollFailuresRef.current.delete(job.localId);
                setDownloads((prev) =>
                  prev.map((d) =>
                    d.localId === job.localId
                      ? {
                          ...d,
                          status: "failed",
                          message: "Server connection lost",
                          errorDetail:
                            "Unable to reach the server to check download status. Please ensure the backend is running.",
                        }
                      : d
                  )
                );
                setTimeout(() => {
                  setDownloads((prev) => prev.filter((d) => d.localId !== job.localId));
                }, 4000);
              } else if (fails >= 3) {
                setDownloads((prev) =>
                  prev.map((d) =>
                    d.localId === job.localId && (d.status === "queued" || d.status === "processing")
                      ? { ...d, message: "Reconnecting to server…" }
                      : d
                  )
                );
              }
            }
          }
        })
      );
    };

    void pollActiveJobs();
    const interval = setInterval(pollActiveJobs, 1000);

    return () => clearInterval(interval);
  }, [token, hasActiveQueueJobs, fetchMoreVideos, setDownloads, handleStartSubtitle, handleStartUpscale]);

  useEffect(() => {
    if (!token) return;
    setVideos([]);
    setHasMore(true);
    fetchMoreVideos(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // Infinite scroll
  useEffect(() => {
    if (!hasMore || loading) return;
    const observer = new window.IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !fetching) fetchMoreVideos();
      },
      { threshold: 1 }
    );
    const loaderElement = loaderRef.current;
    if (loaderElement) observer.observe(loaderElement);
    return () => {
      if (loaderElement) observer.unobserve(loaderElement);
    };
  }, [hasMore, loading, fetching, fetchMoreVideos]);

  async function handleAddVideo(e: { preventDefault(): void }) {
    e.preventDefault();
    if (!url) return;

    const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const submittedUrl = url;
    const controller = new AbortController();

    setUrl("");
    setError("");

    setDownloads((prev) => [
      ...prev,
      {
        localId,
        url: submittedUrl,
        status: "extracting",
        message: "Extracting video metadata...",
        abortController: controller,
      },
    ]);

    try {
      let realUrl = submittedUrl;
      let title: string | undefined;
      let duration: number | undefined;

      try {
        const res = await extractVideo(token!, submittedUrl, controller.signal);
        if (res.video_url) realUrl = res.video_url;
        if (res.title) title = res.title;
        if (res.duration) duration = res.duration;

        if (title) {
          setDownloads((prev) =>
            prev.map((d) => (d.localId === localId ? { ...d, title } : d))
          );
        }

        if (res.job_id) {
          setDownloads((prev) =>
            prev.flatMap((d) =>
              d.localId === localId ? queuedDownloadRows(d, res) : [d]
            )
          );
          return;
        }
      } catch {}

      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId ? { ...d, status: "adding", message: "Saving video..." } : d
        )
      );

      try {
        await addVideo(token!, realUrl, title, duration);
      } catch (addErr: unknown) {
        const axiosErr = addErr as { response?: { status?: number } };
        if (axiosErr?.response?.status !== 409) throw addErr;
      }

      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId
            ? { ...d, title: title || d.url, status: "done", message: "Video added!" }
            : d
        )
      );
      setVideos([]);
      setHasMore(true);
      fetchMoreVideos(true);

      setTimeout(() => {
        setDownloads((prev) => prev.filter((d) => d.localId !== localId));
      }, 4000);
    } catch (err: unknown) {
      const isAbort =
        (err as { name?: string; code?: string })?.name === "CanceledError" ||
        (err as { code?: string })?.code === "ERR_CANCELED";
      if (isAbort) return;
      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId ? { ...d, status: "failed", message: "Failed to add video" } : d
        )
      );
      setTimeout(() => {
        setDownloads((prev) => prev.filter((d) => d.localId !== localId));
      }, 4000);
      setError("Failed to add video");
    }
  }

  async function handleDeleteAll() {
    try {
      await Promise.all(videos.map((v) => deleteVideo(token!, v.id)));
      setVideos([]);
      setHasMore(false);
      setShowDeleteAllDialog(false);
    } catch {
      setError("Failed to delete all videos");
    }
  }

  async function handleDownloadAll() {
    if (downloadingAll || videos.length === 0 || !token) return;
    setDownloadingAll(true);
    for (const v of videos) {
      try {
        const blob = await downloadVideo(token, v.id);
        downloadBlob(blob, videoDownloadName(v, "video"));
        await new Promise((r) => setTimeout(r, 400));
      } catch {}
    }
    setDownloadingAll(false);
  }

  async function handleDelete(id: number) {
    setDeleteId(id);
    setShowDialog(true);
  }

  async function confirmDelete() {
    if (deleteId == null) return;
    try {
      await deleteVideo(token!, deleteId);
      setVideos([]);
      setHasMore(true);
      fetchMoreVideos(true);
      setDeleteId(null);
      setShowDialog(false);
    } catch {
      setError("Failed to delete video");
    }
  }

  const handleLoadedMetadata = useCallback((videoId: number, width: number, height: number) => {
    setVideoMeta((prev) => {
      if (prev[videoId]?.height === height && prev[videoId]?.width === width) return prev;
      return { ...prev, [videoId]: { width, height } };
    });
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">Loading...</div>
    );
  }

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />

      <div className="max-w-6xl mx-auto px-4 sm:px-6">
        <AddVideoForm url={url} setUrl={setUrl} onSubmit={handleAddVideo} />

        {error && <div className="text-red-500 mb-4">{error}</div>}

        <ActivePipelinesDashboard
          downloads={downloads}
          translateJobs={translateJobs}
          enhanceJobs={enhanceJobs}
          onCancelDownload={(dl) => {
            if (dl.abortController) dl.abortController.abort();
            if (dl.jobId && token) void cancelJob(token, dl.jobId);
            setDownloads((prev) => prev.filter((d) => d.localId !== dl.localId));
          }}
          onCancelTranslate={(localId) => cancelLocalTranslateJob(localId, cancelTranslateJob)}
          onCancelEnhance={(localId) => cancelLocalEnhanceJob(localId, cancelEnhanceJob)}
          onDismissTranslate={(localId) => removeTranslateJob(localId)}
          onDismissEnhance={(localId) => removeEnhanceJob(localId)}
        />

        {/* Bulk actions */}
        {videos.length > 0 && (
          <div className="flex items-center justify-between mb-4">
            <span className="text-sm text-gray-500">
              {videos.length} video{videos.length !== 1 ? "s" : ""}
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleDownloadAll}
                disabled={downloadingAll}
                className="border-white/10 bg-transparent hover:bg-indigo-500/10 hover:text-white text-gray-300 rounded-xl text-xs gap-1.5"
              >
                {downloadingAll ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Download className="w-3.5 h-3.5" />
                )}
                {downloadingAll ? "Downloading…" : "Download All"}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowDeleteAllDialog(true)}
                className="border-red-500/20 bg-transparent hover:bg-red-500/10 hover:text-red-300 text-gray-400 rounded-xl text-xs gap-1.5"
              >
                <Trash2 className="w-3.5 h-3.5" />
                Delete All
              </Button>
            </div>
          </div>
        )}

        {/* Video Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-8">
          {videos.length === 0 && !loading && (
            <div className="text-gray-500 col-span-3">No videos found.</div>
          )}
          {videos.map((video) => (
            <VideoCard
              key={`${video.id}-${video.url}`}
              video={video}
              meta={videoMeta[video.id]}
              enhanceJobs={enhanceJobs}
              translateJobs={translateJobs}
              actionPendingId={actionPendingId}
              onLoadedMetadata={handleLoadedMetadata}
              onStartUpscale={handleStartUpscale}
              onStartSubtitle={handleStartSubtitle}
              onDelete={handleDelete}
            />
          ))}
        </div>
        <div ref={loaderRef} className="h-8" />

        <DeleteDialogs
          showDialog={showDialog}
          setShowDialog={setShowDialog}
          onConfirmDelete={confirmDelete}
          showDeleteAllDialog={showDeleteAllDialog}
          setShowDeleteAllDialog={setShowDeleteAllDialog}
          onConfirmDeleteAll={handleDeleteAll}
          totalVideos={videos.length}
        />
      </div>
    </div>
  );
}
