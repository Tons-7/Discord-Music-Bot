"use client";

import { useState } from "react";
import { getSDK } from "@/lib/discord-sdk";
import { useToast } from "./Toast";

export default function InviteButton() {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    const sdk = getSDK();
    if (!sdk || busy) return;
    setBusy(true);
    try {
      await sdk.commands.openInviteDialog();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Can't open invite dialog";
      toast(msg, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={onClick}
      disabled={busy}
      aria-label="Invite friends to listen"
      className="w-8 h-8 rounded-full bg-black/40 backdrop-blur-sm text-white/80 hover:text-white hover:bg-black/60 flex items-center justify-center transition-colors disabled:opacity-50"
    >
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M18 9v3m0 0v3m0-3h3m-3 0h-3M13 7a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0H3z" />
      </svg>
    </button>
  );
}
