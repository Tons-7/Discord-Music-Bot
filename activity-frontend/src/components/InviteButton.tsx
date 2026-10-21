"use client";

import { useState } from "react";
import { getSDK } from "@/lib/discord-sdk";
import { useToast } from "./Toast";
import IconButton from "./ui/IconButton";
import { InviteIcon } from "./ui/icons";

export default function InviteButton() {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);

  const onClick = () => {
    const sdk = getSDK();
    if (!sdk || busy) return;
    setBusy(true);
    sdk.commands.openInviteDialog()
      .catch(() => toast("Couldn't open the invite dialog", "error"))
      .finally(() => setBusy(false));
  };

  return (
    <IconButton
      label="Invite friends to listen"
      size="md"
      onClick={onClick}
      disabled={busy}
      className="bg-surface-1/60 backdrop-blur-sm text-fg/80 hover:text-fg hover:bg-surface-1/80"
    >
      <InviteIcon />
    </IconButton>
  );
}
