"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs } from "../jobs-context";
import { createLocalId, queuedDownloadRows } from "../job-utils";
import {
  extractVideo,
  getSearch,
  moreSearchResults,
  playSearchResult,
  startSearch,
  type PlayData,
  type SearchData,
  type SearchResultItem,
} from "../api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Check, Download, ExternalLink, Loader2, Play, Search } from "lucide-react";
import Navbar from "@/components/Navbar";
import SearchPlayer from "@/components/SearchPlayer";

const POLL_MS = 2000;
const MAX_DESCRIPTION_CHARS = 500;

const PHASE_LABELS: Record<string, string> = {
  planning: "Planning search queries…",
  searching: "Searching the web…",
  ranking: "Ranking results against your description…",
  comparing: "Checking results for the same video on other sites…",
};

function formatDuration(seconds: number | null): string {
  if (!seconds) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function siteName(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || fallback;
}

type QueueState = "queuing" | "queued" | "failed";

type Playing = {
  result: SearchResultItem;
  play: PlayData | null;
  error: string;
};

export default function SearchPage() {
  const { token, loading } = useAuth();
  const router = useRouter();
  const { setDownloads } = useJobs();

  const [description, setDescription] = useState("");
  const [search, setSearch] = useState<SearchData | null>(null);
  const [error, setError] = useState("");
  const [queueStates, setQueueStates] = useState<Record<string, QueueState>>({});
  const [playing, setPlaying] = useState<Playing | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const searchId = search?.search_id;
  const running = search?.status === "running";

  // Poll while the backend works on the current step (first page or "more").
  useEffect(() => {
    if (!token || !searchId || !running) return;
    pollRef.current = setInterval(async () => {
      try {
        setSearch(await getSearch(token, searchId));
      } catch (err) {
        setError(errorMessage(err, "Lost track of this search. Start a new one."));
        setSearch((prev) => (prev ? { ...prev, status: "failed" } : prev));
      }
    }, POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [token, searchId, running]);

  async function handleSearch(e: { preventDefault(): void }) {
    e.preventDefault();
    const text = description.trim();
    if (!text || !token) return;
    setError("");
    setQueueStates({});
    try {
      setSearch(await startSearch(token, text));
    } catch (err) {
      setError(errorMessage(err, "Search failed to start."));
    }
  }

  async function handleMore() {
    if (!token || !searchId) return;
    setError("");
    try {
      setSearch(await moreSearchResults(token, searchId));
    } catch (err) {
      setError(errorMessage(err, "Could not load more results."));
    }
  }

  // Ask the backend how to play a result, then show it in the player dialog.
  async function handlePlay(result: SearchResultItem) {
    if (!token) return;
    setPlaying({ result, play: null, error: "" });
    try {
      const play = await playSearchResult(token, result.url);
      setPlaying((prev) => (prev?.result.url === result.url ? { ...prev, play } : prev));
    } catch (err) {
      const message = errorMessage(err, "This video cannot be played here.");
      setPlaying((prev) => (prev?.result.url === result.url ? { ...prev, error: message } : prev));
    }
  }

  // Send a result to the download queue; it shows up on the Download page.
  async function handleDownload(result: SearchResultItem) {
    if (!token) return;
    setQueueStates((prev) => ({ ...prev, [result.url]: "queuing" }));
    try {
      const res = await extractVideo(token, result.url);
      const row = {
        localId: createLocalId(),
        url: result.url,
        title: result.title,
        status: "queued" as const,
        message: "Video queued for processing.",
      };
      setDownloads((prev) => [...prev, ...queuedDownloadRows(row, res)]);
      setQueueStates((prev) => ({ ...prev, [result.url]: "queued" }));
    } catch {
      setQueueStates((prev) => ({ ...prev, [result.url]: "failed" }));
    }
  }

  const results = search?.results ?? [];

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />

      <div className="max-w-5xl mx-auto px-4 sm:px-6">
        <form
          onSubmit={handleSearch}
          className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5"
        >
          <label htmlFor="description" className="block text-white font-medium mb-1">
            Describe the video you want to watch
          </label>
          <p className="text-gray-400 text-sm mb-4">
            Any kind of video works. Name the people, the topic or what happens in it.
          </p>
          <textarea
            id="description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={MAX_DESCRIPTION_CHARS}
            rows={3}
            className="w-full bg-white/5 border border-white/10 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500 resize-none"
            placeholder="What kind of video are you looking for?"
          />
          <div className="flex items-center justify-between mt-4 gap-4">
            <span className="text-xs text-gray-500">
              {description.length}/{MAX_DESCRIPTION_CHARS}
            </span>
            <Button
              type="submit"
              disabled={!description.trim() || running}
              className="bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl px-6"
            >
              {running ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Search className="w-4 h-4 mr-2" />}
              Search
            </Button>
          </div>
          {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
        </form>

        {search && (
          <div className="mb-6 text-sm text-gray-400 space-y-1">
            {running && (
              <p className="flex items-center gap-2 text-indigo-300">
                <Loader2 className="w-4 h-4 animate-spin" />
                {PHASE_LABELS[search.phase ?? ""] ?? "Working…"}
              </p>
            )}
            {search.status === "failed" && search.error && <p className="text-red-400">{search.error}</p>}
            {search.queries.length > 0 && <p>Searched for: {search.queries.join(" · ")}</p>}
            {!search.ranked_by_llm && (
              <p className="text-amber-400">The ranking model was unavailable, so results are in source order.</p>
            )}
            {search.status === "done" && results.length === 0 && <p>No matching videos found. Try describing it differently.</p>}
          </div>
        )}

        {results.length > 0 && (
          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {results.map((result) => {
              const state = queueStates[result.url];
              return (
                <div key={result.url} className="glass-panel rounded-3xl overflow-hidden flex flex-col">
                  <a href={result.url} target="_blank" rel="noopener noreferrer" className="relative block aspect-video bg-white/5">
                    {result.thumbnail && (
                      <Image
                        src={result.thumbnail}
                        alt={result.title}
                        fill
                        unoptimized
                        className="object-cover"
                        sizes="(max-width: 640px) 100vw, (max-width: 1024px) 50vw, 33vw"
                      />
                    )}
                    {result.duration ? (
                      <span className="absolute bottom-2 right-2 bg-black/75 text-xs px-2 py-0.5 rounded-md">
                        {formatDuration(result.duration)}
                      </span>
                    ) : null}
                  </a>
                  <div className="p-4 flex flex-col gap-2 flex-1">
                    <a
                      href={result.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-medium leading-snug hover:text-indigo-300 line-clamp-2"
                    >
                      {result.title}
                    </a>
                    <p className="text-xs text-gray-500 flex items-center gap-1">
                      <ExternalLink className="w-3 h-3" />
                      {siteName(result.url)}
                    </p>
                    {result.reason && <p className="text-sm text-gray-300">{result.reason}</p>}
                    <div className="mt-auto pt-2 flex flex-col gap-2">
                      <Button
                        onClick={() => handlePlay(result)}
                        className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
                      >
                        <Play className="w-4 h-4 mr-2" /> Play
                      </Button>
                      {state === "queued" ? (
                        <Link href="/" className="text-sm text-emerald-400 flex items-center gap-1">
                          <Check className="w-4 h-4" /> Queued. See it on the Download page.
                        </Link>
                      ) : (
                        <Button
                          onClick={() => handleDownload(result)}
                          disabled={state === "queuing"}
                          variant="outline"
                          className="w-full border-white/10 bg-white/5 hover:bg-white/10 text-white rounded-xl"
                        >
                          {state === "queuing" ? (
                            <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                          ) : (
                            <Download className="w-4 h-4 mr-2" />
                          )}
                          {state === "failed" ? "Retry download" : "Download"}
                        </Button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {search && search.status !== "running" && search.has_more && results.length > 0 && (
          <div className="flex justify-center mt-8">
            <Button onClick={handleMore} className="bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl px-8">
              More results
            </Button>
          </div>
        )}
        <Dialog open={playing !== null} onOpenChange={(open) => !open && setPlaying(null)}>
          <DialogContent className="border-white/10 bg-gray-950 text-white rounded-3xl p-4 sm:p-6 sm:max-w-4xl">
            <DialogHeader>
              <DialogTitle className="text-base sm:text-lg leading-snug pr-6 line-clamp-2">
                {playing?.result.title}
              </DialogTitle>
            </DialogHeader>
            {playing?.play && <SearchPlayer play={playing.play} title={playing.result.title} />}
            {playing && !playing.play && !playing.error && (
              <div className="aspect-video rounded-xl bg-black/60 flex items-center justify-center gap-2 text-indigo-300 text-sm">
                <Loader2 className="w-4 h-4 animate-spin" /> Finding the video stream…
              </div>
            )}
            {playing?.error && (
              <div className="aspect-video rounded-xl bg-black/60 flex flex-col items-center justify-center gap-3 text-sm text-center px-6">
                <p className="text-red-300">{playing.error}</p>
                <a
                  href={playing.result.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-indigo-300 hover:text-indigo-200 flex items-center gap-1"
                >
                  <ExternalLink className="w-4 h-4" /> Open on the site
                </a>
              </div>
            )}
          </DialogContent>
        </Dialog>

        {search?.status === "running" && results.length > 0 && (
          <div className="flex justify-center mt-8 text-indigo-300 text-sm items-center gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            {PHASE_LABELS[search.phase ?? ""] ?? "Loading more…"}
          </div>
        )}
      </div>
    </div>
  );
}
