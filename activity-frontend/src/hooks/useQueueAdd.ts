"use client";

import { useRef, useState } from "react";
import { useGuildId } from "@/components/GuildStateProvider";
import { useToast } from "@/components/Toast";
import { apiFetch, errorMessage } from "@/lib/api";
import { duplicateMessage, type QueueAddResult } from "@/lib/queueActions";

export type AddStatus = "default" | "pending" | "added";

// How long a row shows its "added" check before it can be added again.
const ADDED_MS = 2500;

function without(set: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(set);
  next.delete(key);
  return next;
}

/**
 * The add-to-queue flow every panel shares: per-row pending and added state (rows never
 * block each other), the duplicate notice, and an error toast when the add fails.
 */
export function useQueueAdd() {
  const { guildId } = useGuildId();
  const { toast } = useToast();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [added, setAdded] = useState<ReadonlySet<string>>(new Set());
  // Guards a second click that lands before the pending state renders.
  const inflight = useRef(new Set<string>());

  const markAdded = (keys: string[]) => {
    setAdded(prev => new Set([...prev, ...keys]));
    setTimeout(() => setAdded(prev => {
      const next = new Set(prev);
      for (const k of keys) next.delete(k);
      return next;
    }), ADDED_MS);
  };

  const status = (key: string): AddStatus =>
    added.has(key) ? "added" : pending.has(key) ? "pending" : "default";

  /** Adds `query` (a song URL) to the queue. `key` identifies the row, `title` names it in toasts. */
  const add = (key: string, title: string, query: string = key) => {
    if (inflight.current.has(key) || added.has(key)) return;
    inflight.current.add(key);
    setPending(prev => new Set(prev).add(key));
    apiFetch<QueueAddResult>(`/api/guild/${guildId}/queue/add`, {
      method: "POST",
      body: JSON.stringify({ query }),
    }).then(
      (res) => {
        inflight.current.delete(key);
        setPending(prev => without(prev, key));
        if (res.duplicate) {
          toast(duplicateMessage(res, title), "info");
          return;
        }
        markAdded([key]);
        toast(`Added "${title}"`, "success");
      },
      (e: unknown) => {
        inflight.current.delete(key);
        setPending(prev => without(prev, key));
        toast(errorMessage(e, `Couldn't add "${title}"`), "error");
      },
    );
  };

  return { add, status, markAdded };
}
