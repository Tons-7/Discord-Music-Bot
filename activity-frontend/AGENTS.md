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
- Panel data goes through SWR (global fetcher = `apiFetch`); mutations use optimistic `mutate` + revalidate. Lyrics are cached per song (immutable) and preloaded ~1.5s after song start.
- Touch first: hover-revealed actions need `pointer-coarse:opacity-100`; icon buttons use `ui/IconButton` (enlarged coarse hit area + aria-label); reordering uses dnd-kit (MouseSensor distance 6 + TouchSensor delay 250) — suppress the post-drag click via a `dragOccurred` ref.
- Popovers use `ui/Popover` (portal + fixed positioning) — absolutely-positioned menus get clipped by the mobile scroll containers.
- Text inputs: `text-base sm:text-sm` (16px on mobile or iOS zooms the page on focus).

# Deploying UI changes

Chunk filenames are NOT content-hashed across builds; Discord's activity proxy + Cloudflare cache them. `next.config.ts` therefore sets a per-build `assetPrefix` (`/v-<stamp>`), and the backend strips that segment (`FrontendStaticFiles` in `activity/app.py`). A local `npm run build` is never served — deploy with `docker compose build bot && docker compose up -d`, then verify the `build <stamp>` line on the Connecting screen before debugging "change didn't apply".
