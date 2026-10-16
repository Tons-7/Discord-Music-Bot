<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Audio playback

`useAudioPlayer` (`src/hooks/useAudioPlayer.ts`) owns the single `<audio>` element. It sets `src = /api/guild/{id}/stream?token=...` whenever `currentWebpageUrl` or `eventVersion` changes, then waits for the `canplay` event before calling `play()`. If playback hangs on song change, suspect the backend stream endpoint (`activity/routes/stream_routes.py`) — it must stream chunks, not buffer the upstream body, since Chromium sends `Range: bytes=0-` for media elements.

Other invariants in the hook: `play()` rejections with `NotAllowedError` set `blocked` (UI shows a tap-to-play overlay calling `resume()`); a null `currentWebpageUrl` (remote stop) must tear the element down or other clients keep hearing audio; error recovery resumes from `positionRef`, not `currentTime` (a failed seek reverts the element's clock). Bass/Treble/8D run through a lazily-created Web Audio chain — once `createMediaElementSource` exists the element is permanently routed through it, so it is only built when one of those effects is first used.

# Position is never React state

The per-second WS position tick lives in a ref + subscription (`ServerPosition` from `useWebSocket`). Display components read it via `requestAnimationFrame` writing `transform: scaleX()` (`SeekBar`/`ProgressStrip`/`LiveTime` in `src/components/ui/`); logic (drift correction) subscribes. Never put playback position into `useState` — the whole tree re-rendering per second was the app's original perf problem. The unified getter is `audio.ready ? audio.positionRef.current : serverPos.ref.current.position`.

# Conventions

- React Compiler is enabled — don't hand-memoize new code unless profiling says so.
- Prefer `useEffectEvent` over ref-mirroring for "read latest value inside a stable Effect/listener" (see `useAudioPlayer`/`useWebSocket`/`useRichPresence`).
- Side panels stay mounted inside `<Activity>` (`Panels` in `Dashboard.tsx`) — hidden panels keep local state/scroll with effects unmounted. Don't add unmount-based cleanup assumptions to panel components.
- Panel data goes through SWR (global fetcher = `apiFetch`); mutations use optimistic `mutate` + revalidate. Lyrics are cached per song (immutable) and preloaded ~1.5s after song start.
- Touch first: hover-revealed actions need `pointer-coarse:opacity-100`; icon buttons use `ui/IconButton` (enlarged coarse hit area + aria-label); reordering uses dnd-kit (MouseSensor distance 6 + TouchSensor delay 250) — suppress the post-drag click via a `dragOccurred` ref.
- Popovers use `ui/Popover` (portal + fixed positioning) — absolutely-positioned menus get clipped by the mobile scroll containers.
- Text inputs: `text-base sm:text-sm` (16px on mobile or iOS zooms the page on focus).

# Deploying UI changes

Chunk filenames are NOT content-hashed across builds; Discord's activity proxy + Cloudflare cache them. `next.config.ts` therefore sets a per-build `assetPrefix` (`/v-<stamp>`), and the backend strips that segment (`FrontendStaticFiles` in `activity/app.py`). A local `npm run build` is never served — deploy with `docker compose build bot && docker compose up -d`, then verify the `build <stamp>` line on the Connecting screen before debugging "change didn't apply".

# UI rules

Condensed from the project's UI/UX guide; follow them for every UI change.

- **Fit the product.** Use the theme tokens in `globals.css` (`surface-*`, `accent`, `accent-glow`, `muted`, `success`, `danger`) and the existing components (`SongRow`, `IconButton`, `Popover`, `KeyCap`, `ui/icons`). No new aesthetic, no raw hex or rgba in components: every colour must follow the listener's theme. Dark only: Activities run inside Discord.
- **Themes** are per listener, saved on the server per Discord account (`useTheme` → `/api/me/preferences`); the localStorage copy only paints the theme before the server answers. Each theme is a `[data-theme]` block in `globals.css` plus an entry in `lib/themes.ts`; `@theme` must stay non-`inline` so utilities read the variables. New themes keep `muted` at 4.5:1 or better against `surface-1` and `surface-2`.
- **Quiet by default.** Neutral surfaces and one accent; status colours only where earned (`success` done, `danger` destructive/error).
- **Rows** read as a strong title, then muted details joined by " · ". Siblings share height, padding and alignment; the whole row is the click target.
- **Type.** Body and titles `text-sm` (14px), secondary details `text-xs` (12px). Nothing smaller, except badges (`text-[11px]`), the PiP tile and the mobile tab labels.
- **Time, duration and index columns** use `tabular-nums` with a `min-w-*`, never a fixed width that long values overflow.
- **Feedback on every action at once:** per-row pending/added state plus a toast. Never block other rows while one action is in flight.
- **Loading** uses skeletons shaped like the content (`ui/Skeleton`), one indicator at a time, no flash of empty state.
- **Errors** in plain language, with the fix as a button where there is one. Destructive actions ask for confirmation.
- **Empty states** are minimal: a quiet icon and the next action.
- **Motion** is short (150–250ms). `globals.css` honours `prefers-reduced-motion`; JS-driven motion (`scrollIntoView`, marquee) must check it too.
- **Shortcuts** are listed in `SHORTCUTS` (`useKeyboardShortcuts.ts`) and shown as `KeyCap`s in the shortcuts popover; add new ones there.
- **Words.** Short labels, plain language, the same name for each thing everywhere (Queue, Playlists, Favorites, History).
