"use client";

import { useState, useEffect } from "react";
import { getSDK } from "@/lib/discord-sdk";

export const LayoutMode = {
  Focused: 0,
  PiP: 1,
  Grid: 2,
} as const;
export type LayoutMode = typeof LayoutMode[keyof typeof LayoutMode];

export function useLayoutMode(): LayoutMode {
  const [mode, setMode] = useState<LayoutMode>(LayoutMode.Focused);

  useEffect(() => {
    const sdk = getSDK();
    if (!sdk) return;

    const handler = ({ layout_mode }: { layout_mode: number }) => {
      setMode(
        (layout_mode === LayoutMode.Focused || layout_mode === LayoutMode.PiP || layout_mode === LayoutMode.Grid
          ? layout_mode
          : LayoutMode.Focused) as LayoutMode
      );
    };

    sdk.subscribe("ACTIVITY_LAYOUT_MODE_UPDATE", handler).catch(() => {});
    return () => {
      sdk.unsubscribe("ACTIVITY_LAYOUT_MODE_UPDATE", handler).catch(() => {});
    };
  }, []);

  return mode;
}
