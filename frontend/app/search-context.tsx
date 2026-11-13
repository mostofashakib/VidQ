"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { useAuth } from "./auth-context";
import { getSearch, moreSearchResults, startSearch, type SearchData } from "./api";

// The current search lives above the pages, so switching tabs never drops it:
// the backend keeps working and this provider keeps polling. The search id is
// saved too, so a reload reconnects to a search the backend still holds.

const POLL_MS = 2000;
const STORAGE_KEY = "vidq_search";

export type QueueState = "queuing" | "queued" | "failed";

interface SearchContextValue {
  description: string;
  setDescription: Dispatch<SetStateAction<string>>;
  search: SearchData | null;
  error: string;
  queueStates: Record<string, QueueState>;
  setQueueStates: Dispatch<SetStateAction<Record<string, QueueState>>>;
  start: (text: string) => Promise<void>;
  loadMore: () => Promise<void>;
}

const SearchContext = createContext<SearchContextValue | null>(null);

export function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || fallback;
}

function saveSearch(searchId: string, description: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ searchId, description }));
  } catch {
    // Storage is a convenience; the search still works without it.
  }
}

function savedSearch(): { searchId: string; description: string } | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function SearchProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth();
  const [description, setDescription] = useState("");
  const [search, setSearch] = useState<SearchData | null>(null);
  const [error, setError] = useState("");
  const [queueStates, setQueueStates] = useState<Record<string, QueueState>>({});

  // Reconnect to the saved search after a reload, if the backend still has it.
  useEffect(() => {
    if (!token) return;
    const saved = savedSearch();
    if (!saved?.searchId) return;
    setDescription((current) => current || saved.description);
    getSearch(token, saved.searchId)
      .then((data) => setSearch((current) => current ?? data))
      .catch(() => {
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch {
          // Nothing to clean up.
        }
      });
  }, [token]);

  const searchId = search?.search_id;
  const running = search?.status === "running";

  // Poll while the backend works on the current step, whichever tab is open.
  useEffect(() => {
    if (!token || !searchId || !running) return;
    const timer = setInterval(async () => {
      try {
        setSearch(await getSearch(token, searchId));
      } catch (err) {
        setError(errorMessage(err, "Lost track of this search. Start a new one."));
        setSearch((prev) => (prev ? { ...prev, status: "failed" } : prev));
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [token, searchId, running]);

  const start = useCallback(
    async (text: string) => {
      if (!token) return;
      setError("");
      setQueueStates({});
      try {
        const data = await startSearch(token, text);
        setSearch(data);
        saveSearch(data.search_id, text);
      } catch (err) {
        setError(errorMessage(err, "Search failed to start."));
      }
    },
    [token],
  );

  const loadMore = useCallback(async () => {
    if (!token || !searchId) return;
    setError("");
    try {
      setSearch(await moreSearchResults(token, searchId));
    } catch (err) {
      setError(errorMessage(err, "Could not load more results."));
    }
  }, [token, searchId]);

  return (
    <SearchContext.Provider
      value={{ description, setDescription, search, error, queueStates, setQueueStates, start, loadMore }}
    >
      {children}
    </SearchContext.Provider>
  );
}

export function useSearch() {
  const ctx = useContext(SearchContext);
  if (!ctx) throw new Error("useSearch must be used inside SearchProvider");
  return ctx;
}
