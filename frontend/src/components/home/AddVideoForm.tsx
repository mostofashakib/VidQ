"use client";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

interface AddVideoFormProps {
  url: string;
  setUrl: (url: string) => void;
  onSubmit: (e: React.FormEvent) => void;
}

export function AddVideoForm({ url, setUrl, onSubmit }: AddVideoFormProps) {
  function handlePaste(e: React.ClipboardEvent<HTMLInputElement>) {
    setUrl(e.clipboardData.getData("text").trim());
    e.preventDefault();
  }

  return (
    <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5 transform transition-all hover:scale-[1.01] duration-500">
      <form onSubmit={onSubmit} className="flex flex-col gap-4">
        <div className="flex flex-col sm:flex-row gap-4">
          <Input
            placeholder="Video URL (YouTube, Vimeo, etc)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onPaste={handlePaste}
            className="flex-1 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
          />
          <Button
            type="submit"
            disabled={!url}
            className="sm:w-auto w-full h-14 px-8 font-semibold rounded-xl bg-linear-to-r from-indigo-500 to-purple-600 hover:from-indigo-400 hover:to-purple-500 transition-all border-none shadow-lg shadow-indigo-500/25 text-white disabled:opacity-50 cursor-pointer"
          >
            Add Video
          </Button>
        </div>
      </form>
    </div>
  );
}
