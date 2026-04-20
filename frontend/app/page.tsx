"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "./auth-context";
import {
  addVideo,
  listVideos,
  listCategories,
  deleteVideo,
  extractVideo,
  enqueueVideo,
  getQueueStatus,
} from "./api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import Image from "next/image";
import { Trash, Download } from "lucide-react";

interface Video {
  id: number;
  url: string;
  category: string;
  title?: string;
  created_at: string;
  duration?: number;
  thumbnail?: string;
}

const PAGE_SIZE = 20;

export default function HomePage() {
  const { token, logout, loading } = useAuth();
  const router = useRouter();
  const [videos, setVideos] = useState<Video[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<string>("all");
  const [url, setUrl] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(true);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  const loaderRef = useRef<HTMLDivElement | null>(null);
  const [fetching, setFetching] = useState(false);
  const [addLoading, setAddLoading] = useState(false);
  const [queuedJob, setQueuedJob] = useState<{ job_id: string; message: string; status?: string } | null>(null);

  async function fetchCategories() {
    try {
      const cats = await listCategories(token!);
      setCategories(["all", ...cats.filter((c: string) => c !== "all")]);
    } catch {
      setError("Failed to load categories");
    }
  }

  const fetchMoreVideos = useCallback(
    async (reset = false) => {
      if (!token) return;
      setFetching(true);
      try {
        const skip = reset ? 0 : videos.length;
        const newVideos = await listVideos(
          token,
          selectedCategory,
          skip,
          PAGE_SIZE
        );
        setVideos((prev) => (reset ? newVideos : [...prev, ...newVideos]));
        setHasMore(newVideos.length === PAGE_SIZE);
      } catch {
        setError("Failed to load videos");
      } finally {
        setFetching(false);
      }
    },
    [token, selectedCategory, videos.length]
  );

  useEffect(() => {
    if (!loading && !token) {
      router.replace("/login");
    }
  }, [token, loading, router]);

  // Handle queue polling
  useEffect(() => {
    if (!token || !queuedJob || !queuedJob.job_id) return;

    let polling = true;
    const interval = setInterval(async () => {
      if (!polling) return;
      try {
        const statusData = await getQueueStatus(token, queuedJob.job_id);
        
        if (statusData.status === "done") {
          polling = false;
          clearInterval(interval);
          setQueuedJob((prev) => prev ? { ...prev, message: "Video ready!", status: "done" } : null);
          
          // Refresh list to show new video
          setTimeout(() => {
            setVideos([]);
            setHasMore(true);
            fetchMoreVideos(true);
            // Clear job status after 5s
            setTimeout(() => setQueuedJob(null), 5000);
          }, 500);
        } else if (statusData.status === "failed") {
          polling = false;
          clearInterval(interval);
          setQueuedJob((prev) => prev ? { ...prev, message: `Failed: ${statusData.error}`, status: "failed" } : null);
        } else {
          // Update status message
          const msg = statusData.status === "processing" 
            ? "Downloading and processing video..." 
            : `Queued (Position: ${statusData.queue_position ?? "unknown"})`;
          
          setQueuedJob((prev) => prev ? { ...prev, message: msg } : null);
        }
      } catch (err) {
        console.error("Polling error:", err);
      }
    }, 3000);

    return () => {
      polling = false;
      clearInterval(interval);
    };
  }, [token, queuedJob?.job_id, fetchMoreVideos]);

  useEffect(() => {
    if (!token) return;
    setVideos([]);
    setHasMore(true);
    fetchCategories();
    fetchMoreVideos(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, selectedCategory]);

  // Infinite scroll
  useEffect(() => {
    if (!hasMore || loading) return;
    const observer = new window.IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !fetching) {
          fetchMoreVideos();
        }
      },
      { threshold: 1 }
    );
    if (loaderRef.current) observer.observe(loaderRef.current);
    return () => {
      if (loaderRef.current) observer.unobserve(loaderRef.current);
    };
    // eslint-disable-next-line
  }, [loaderRef.current, hasMore, loading, fetching]);

  async function handleAddVideo(e: React.FormEvent) {
    e.preventDefault();
    if (!url || !category) return;
    setAddLoading(true);
    setError("");
    setQueuedJob(null);
    try {
      let realUrl = url;
      let title: string | undefined = undefined;
      let duration: number | undefined = undefined;
      try {
        const res = await extractVideo(token!, url);
        if (res.video_url) realUrl = res.video_url;
        if (res.title) title = res.title;
        if (res.duration) duration = res.duration;

        // If the extraction returned a queued job signal (blob stream needing long recording)
        if (res.job_id) {
          setQueuedJob({ job_id: res.job_id, message: res.message || "Video queued for processing." });
          setUrl("");
          setCategory("");
          return;
        }
      } catch {}

      try {
        await addVideo(token!, realUrl, category, title, duration);
      } catch (addErr: any) {
        // 409 = already exists, treat as success
        if (addErr?.response?.status !== 409) throw addErr;
      }
      setUrl("");
      setCategory("");
      setVideos([]);
      setHasMore(true);
      fetchMoreVideos(true);
    } catch (e) {
      console.log("error", e);
      setError("Failed to add video");
    } finally {
      setAddLoading(false);
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text");
    setUrl(pasted.trim());
    e.preventDefault();
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

  function formatDuration(seconds?: number) {
    if (!seconds || isNaN(seconds)) return "Unknown";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        Loading...
      </div>
    );
  }

  return (
    <div className="min-h-screen text-white pb-20">
      <div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
        <h1 
          className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-indigo-400 to-purple-400 cursor-pointer"
          onClick={() => window.location.reload()}
        >
          VideoSearch
        </h1>
        <Button variant="outline" onClick={logout} className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200">
          Logout
        </Button>
      </div>
      <div className="max-w-6xl mx-auto px-4 sm:px-6">
        <div className="glass-panel p-6 md:p-8 rounded-[2rem] mb-12 shadow-2xl shadow-purple-500/5 transform transition-all hover:scale-[1.01] duration-500">
          <form onSubmit={handleAddVideo} className="flex flex-col sm:flex-row gap-4">
            <Input
              placeholder="Video URL (YouTube, Vimeo, etc)"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onPaste={handlePaste}
              className="flex-1 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
            />
            <Input
              placeholder="Category Name"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full sm:w-56 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
            />
            <Button
              type="submit"
              disabled={addLoading || !url || !category}
              className="sm:w-auto w-full h-14 px-8 font-semibold rounded-xl bg-gradient-to-r from-indigo-500 to-purple-600 hover:from-indigo-400 hover:to-purple-500 transition-all border-none shadow-lg shadow-indigo-500/25 text-white"
            >
              {addLoading ? (
                <>
                  <span className="animate-spin mr-2">⏳</span> Processing...
                </>
              ) : (
                "Add Video"
              )}
            </Button>
          </form>
        </div>
        {error && <div className="text-red-500 mb-4">{error}</div>}
        {queuedJob && (
          <div className={`mb-6 p-4 rounded-2xl border transition-colors ${
            queuedJob.status === "done" ? "bg-green-500/10 border-green-500/30 text-green-300" :
            queuedJob.status === "failed" ? "bg-red-500/10 border-red-500/30 text-red-300" :
            "bg-indigo-500/10 border-indigo-500/30 text-indigo-300"
          } flex items-start gap-3`}>
            <span className="text-xl mt-0.5">
              {queuedJob.status === "done" ? "✅" : queuedJob.status === "failed" ? "❌" : "⏳"}
            </span>
            <div className="flex-1">
              <p className={`font-semibold ${
                queuedJob.status === "done" ? "text-green-200" :
                queuedJob.status === "failed" ? "text-red-200" :
                "text-indigo-200"
              }`}>
                {queuedJob.status === "done" ? "Processing Detailed!" : 
                 queuedJob.status === "failed" ? "Processing Failed" : 
                 "Video queued for processing"}
              </p>
              <p className={`text-sm mt-0.5 ${
                queuedJob.status === "done" ? "text-green-300/80" :
                queuedJob.status === "failed" ? "text-red-300/80" :
                "text-indigo-300/80"
              }`}>{queuedJob.message}</p>
              <p className="text-xs text-indigo-400/60 mt-1 font-mono">Job ID: {queuedJob.job_id}</p>
            </div>
            <button onClick={() => setQueuedJob(null)} className="text-indigo-400 hover:text-white transition-colors text-lg leading-none">✕</button>
          </div>
        )}
        <div className="flex justify-center mb-12">
          <Tabs
            value={selectedCategory}
            onValueChange={setSelectedCategory}
            className="w-full max-w-3xl"
          >
            <TabsList className="bg-white/5 border border-white/10 p-1.5 rounded-2xl w-full flex overflow-x-auto hide-scrollbar h-auto">
              {categories.map((cat) => (
                <TabsTrigger 
                  key={cat} 
                  value={cat} 
                  className="capitalize rounded-xl px-6 py-3 text-sm font-medium transition-all data-[state=active]:bg-indigo-500 data-[state=active]:text-white data-[state=active]:shadow-lg data-[state=active]:shadow-indigo-500/30 text-gray-400 hover:text-white"
                >
                  {cat || "all"}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-8">
          {videos.length === 0 && !loading && (
            <div className="text-gray-500 col-span-3">No videos found.</div>
          )}
          {videos.map((video) => (
            <Card
              key={`${video.id}-${video.url}`}
              className="glass-panel overflow-hidden flex flex-col min-h-[360px] min-w-[320px] rounded-3xl border border-white/10 hover:border-indigo-500/30 shadow-xl hover:shadow-indigo-500/20 transform transition-all hover:-translate-y-2 duration-500 group bg-transparent"
            >
              {video.thumbnail && (
                <div className="w-full aspect-video relative overflow-hidden bg-black/40">
                  <Image
                    src={video.thumbnail}
                    alt={video.title || "Video thumbnail"}
                    fill
                    className="object-cover group-hover:scale-105 transition-transform duration-700 ease-out opacity-90 group-hover:opacity-100"
                    sizes="(max-width: 768px) 100vw, (max-width: 1200px) 50vw, 33vw"
                    priority={true}
                  />
                  <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-transparent to-transparent"></div>
                </div>
              )}
              <CardHeader className="pb-3 pt-5 relative z-10">
                <CardTitle
                  className="text-lg font-bold break-words whitespace-pre-line leading-tight truncate text-white drop-shadow-md"
                  title={video.title}
                >
                  {video.title || "Untitled Video"}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex-1 flex flex-col justify-between z-10 px-6">
                <div className="mb-5 text-xs flex gap-2">
                  <span className="inline-block px-3 py-1.5 bg-white/10 rounded-full text-indigo-300 font-medium tracking-wider uppercase text-[10px] border border-white/5">
                    {video.category}
                  </span>
                  {video.duration !== undefined && video.duration !== null && (
                    <span className="inline-block px-3 py-1.5 bg-black/30 rounded-full text-gray-300 font-mono text-[10px] border border-white/5">
                      {formatDuration(video.duration)}
                    </span>
                  )}
                </div>
                <div className="flex justify-between items-center rounded-xl overflow-hidden shadow-inner bg-black/40 relative group-hover:shadow-indigo-500/20 transition-all aspect-video">
                  <video
                    className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity duration-300"
                    controls
                    preload="metadata"
                  >
                    <source src={video.url} type="video/mp4" />
                    <source src={video.url} type="video/webm" />
                    Your browser does not support the video tag.
                  </video>
                </div>
              </CardContent>
              <CardFooter className="flex justify-end gap-2 pt-0 pb-5 pr-6 z-10">
                <Button
                  variant="outline"
                  size="icon"
                  title="Download Video"
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      const res = await fetch(video.url);
                      const blob = await res.blob();
                      const url = window.URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      const ext = video.url.split('.').pop()?.split('?')[0] || 'mp4';
                      a.download = video.title ? `${video.title.replace(/[^a-z0-9]/gi, '_').toLowerCase()}.${ext}` : `video-${video.id}.${ext}`;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      window.URL.revokeObjectURL(url);
                    } catch (err) {
                      console.error("Download failed", err);
                      // Fallback
                      window.open(video.url, "_blank");
                    }
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500 hover:text-white border border-indigo-500/20 transition-all shadow hover:shadow-indigo-500/40"
                >
                  <Download className="w-4 h-4" />
                </Button>
                <Button
                  variant="destructive"
                  size="icon"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(video.id);
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all shadow hover:shadow-red-500/40"
                >
                  <Trash className="w-4 h-4" />
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
        <div ref={loaderRef} className="h-8"></div>
        <Dialog open={showDialog} onOpenChange={setShowDialog}>
          <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-[2rem] p-6 sm:p-8 shadow-2xl shadow-black">
            <DialogHeader>
              <DialogTitle className="text-xl">Delete Video</DialogTitle>
            </DialogHeader>
            <div className="text-gray-300 my-2">
              Are you sure you want to delete this video? This action cannot be
              undone.
            </div>
            <DialogFooter className="mt-4 gap-2">
              <Button variant="outline" onClick={() => setShowDialog(false)} className="rounded-xl border-white/10 hover:bg-white/10 hover:text-white text-gray-300">
                Cancel
              </Button>
              <Button onClick={confirmDelete} className="bg-red-500/20 text-red-400 hover:bg-red-500/40 hover:text-white border border-red-500/30 transition-all rounded-xl shadow-lg shadow-red-500/20">
                Delete
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  );
}
