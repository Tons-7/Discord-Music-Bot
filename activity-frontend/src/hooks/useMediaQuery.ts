"use client";

import { useSyncExternalStore } from "react";

// One MediaQueryList per query for the whole app, however many rows subscribe.
const lists = new Map<string, MediaQueryList>();

function list(query: string): MediaQueryList {
  let mq = lists.get(query);
  if (!mq) {
    mq = window.matchMedia(query);
    lists.set(query, mq);
  }
  return mq;
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mq = list(query);
      mq.addEventListener("change", onChange);
      return () => mq.removeEventListener("change", onChange);
    },
    () => list(query).matches,
    () => false,
  );
}
