"use client";

import { useEffect } from "react";
import useSWR from "swr";
import { apiFetch } from "@/lib/api";
import { applyTheme, toTheme, type ThemeId } from "@/lib/themes";

const PREFERENCES = "/api/me/preferences";

type Preferences = { theme: string | null };

/**
 * The listener's theme, saved on the server per Discord account so it follows them across
 * servers and devices. The local copy only paints it before the server answers.
 */
export function useTheme(): [ThemeId, (id: ThemeId) => Promise<void>] {
  const { data, mutate } = useSWR<Preferences>(PREFERENCES);
  const theme = toTheme(data ? data.theme : document.documentElement.dataset.theme);

  useEffect(() => {
    if (data) applyTheme(toTheme(data.theme));
  }, [data]);

  const setTheme = async (id: ThemeId) => {
    applyTheme(id);
    await mutate(
      apiFetch<Preferences>(PREFERENCES, { method: "PUT", body: JSON.stringify({ theme: id }) }),
      { optimisticData: { theme: id }, rollbackOnError: true, revalidate: false },
    );
  };

  return [theme, setTheme];
}
