<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Audio playback

`useAudioPlayer` (`src/hooks/useAudioPlayer.ts`) owns the single `<audio>` element. It sets `src = /api/guild/{id}/stream?token=...` whenever `currentWebpageUrl` or `eventVersion` changes, then waits for the `canplay` event before calling `play()`. If playback hangs on song change, suspect the backend stream endpoint (`activity/routes/stream_routes.py`) — it must stream chunks, not buffer the upstream body, since Chromium sends `Range: bytes=0-` for media elements.
