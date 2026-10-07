"use client";

import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { streamUrl, type PlayData } from "../../app/api";

type SearchPlayerProps = {
  play: PlayData;
  title: string;
};

/** Plays a search result: a relayed file or HLS stream, or the site's own embedded player. */
export default function SearchPlayer({ play, title }: SearchPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || play.mode !== "stream") return;
    setFailed(false);
    const src = streamUrl(play.path);
    if (play.kind === "file" || !Hls.isSupported()) {
      // Safari plays HLS natively; every browser plays a file.
      video.src = src;
      return () => {
        video.removeAttribute("src");
        video.load();
      };
    }
    const hls = new Hls();
    // autoPlay does not start a stream attached through hls.js, so start it here.
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
    });
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) setFailed(true);
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    return () => hls.destroy();
  }, [play]);

  if (play.mode === "embed") {
    return (
      <div className="rounded-xl overflow-hidden bg-black aspect-video">
        <iframe
          src={play.embed_url}
          title={title}
          className="w-full h-full"
          allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
          sandbox="allow-scripts allow-same-origin allow-presentation"
          allowFullScreen
        />
      </div>
    );
  }

  return (
    <div className="rounded-xl overflow-hidden bg-black aspect-video relative">
      <video
        ref={videoRef}
        controls
        autoPlay
        className="w-full h-full object-contain bg-black"
        title={title}
        onError={() => setFailed(true)}
      />
      {failed && (
        <p className="absolute inset-x-0 bottom-12 text-center text-sm text-red-300">
          The video stopped loading. Try again, or open it on the site.
        </p>
      )}
    </div>
  );
}
