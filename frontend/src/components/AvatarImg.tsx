// An `<img>` for a profile picture that walks the candidate URLs from
// avatarSources() -- the CDN directly, then our own proxy -- and renders
// `fallback` only once every one of them has actually failed.
//
// Retrying in here rather than at each call site is what makes the proxy
// fallback worth having: a platform whose CDN starts refusing to be embedded
// cross-origin recovers on its own, everywhere an avatar is drawn, with no
// change at the call sites. See utils/avatar.ts for why the list is ordered
// the way it is.
import { useEffect, useMemo, useState } from "react";
import { avatarSources } from "../utils/avatar";

interface Props {
  src: string | null | undefined;
  alt?: string;
  className?: string;
  style?: React.CSSProperties;
  /** Drawn when there is no picture, or when every candidate URL has failed. */
  fallback?: React.ReactNode;
}

export function AvatarImg({ src, alt = "", className, style, fallback = null }: Props) {
  const sources = useMemo(() => avatarSources(src), [src]);
  const [attempt, setAttempt] = useState(0);

  // A grid row can be recycled onto a different profile while mounted; without
  // this the new picture would inherit the old one's failure count and could
  // skip straight to the fallback.
  useEffect(() => setAttempt(0), [src]);

  if (attempt >= sources.length) return <>{fallback}</>;

  return (
    <img
      // Keyed on the URL so swapping to the next candidate remounts the
      // element -- React reuses a plain <img> on a src change, and a browser
      // that has already errored on it will not always re-fire onError.
      key={sources[attempt]}
      src={sources[attempt]}
      alt={alt}
      className={className}
      style={style}
      referrerPolicy="no-referrer"
      onError={() => setAttempt((n) => n + 1)}
    />
  );
}
