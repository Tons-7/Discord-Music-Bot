import { describe, expect, it } from "vitest";
import { INITIAL_STATE, mergeGuildState, normalizeCurrent, shareSongs } from "./guildState";
import { closeAction, fatalCloseMessage, reconnectDelay } from "./ws";
import { isPlaylistDetailKey, playlistDetailKey, playlistNameError } from "./playlists";
import { bulkAddMessage, duplicateMessage } from "./queueActions";
import type { Song } from "@/types";

const song = (n: number, extra: Partial<Song> = {}): Song => ({
  title: `Song ${n}`, duration: 100 + n, thumbnail: "", uploader: "A", webpage_url: `https://y/${n}`,
  requested_by: "Sam", is_live: false, ...extra,
});

describe("shareSongs", () => {
  it("returns the previous array when nothing changed", () => {
    const prev = [song(1), song(2)];
    expect(shareSongs(prev, [song(1), song(2)])).toBe(prev);
  });
  it("reuses unchanged song objects and replaces changed ones", () => {
    const prev = [song(1), song(2)];
    const next = shareSongs(prev, [song(2), song(3), song(1, { title: "Renamed" })]);
    expect(next[0]).toBe(prev[1]);
    expect(next[2]).not.toBe(prev[0]);
    expect(next[2].title).toBe("Renamed");
  });
});

describe("normalizeCurrent", () => {
  it("strips the live position and keeps identity when only it changed", () => {
    const first = normalizeCurrent(null, { ...song(1), is_paused: false, position: 5 });
    expect(first).not.toHaveProperty("position");
    expect(normalizeCurrent(first, { ...song(1), is_paused: false, position: 9 })).toBe(first);
  });
});

describe("mergeGuildState", () => {
  it("keeps lists a message leaves out", () => {
    const prev = { ...INITIAL_STATE, queue: [song(1)] };
    const next = mergeGuildState(prev, { volume: 50 });
    expect(next.queue).toBe(prev.queue);
    expect(next.volume).toBe(50);
  });
  it("clears the current song", () => {
    const prev = mergeGuildState(INITIAL_STATE, { current: { ...song(1), is_paused: false } });
    expect(mergeGuildState(prev, { current: null }).current).toBeNull();
  });
  it("keeps played songs a message leaves out and shares unchanged ones", () => {
    const prev = mergeGuildState(INITIAL_STATE, { played: [song(1), song(2)] });
    expect(mergeGuildState(prev, { volume: 50 }).played).toBe(prev.played);
    const next = mergeGuildState(prev, { played: [song(1), song(2), song(3)] });
    expect(next.played[0]).toBe(prev.played[0]);
    expect(next.played).toHaveLength(3);
  });
});

describe("WebSocket close handling", () => {
  it("classifies close codes", () => {
    expect(closeAction(4001)).toBe("refresh-token");
    expect(closeAction(4003)).toBe("fatal");
    expect(closeAction(4004)).toBe("fatal");
    expect(closeAction(1006)).toBe("retry");
  });
  it("uses the server's reason, or plain words", () => {
    expect(fatalCloseMessage(4003, "You are banned")).toBe("You are banned");
    expect(fatalCloseMessage(4004, "")).toMatch(/isn't in this server/);
  });
  it("keeps early retries inside the server's 20s grace", () => {
    const fixed = () => 0.5;
    const delays = [0, 1, 2, 3, 4].map(n => reconnectDelay(n, fixed));
    expect(delays).toEqual([500, 1000, 2000, 4000, 5000]);
    expect(delays.reduce((a, b) => a + b, 0)).toBeLessThan(20000);
    expect(reconnectDelay(10, fixed)).toBe(15000);
  });
  it("jitters by at most 20%", () => {
    expect(reconnectDelay(2, () => 0)).toBe(1600);
    expect(reconnectDelay(2, () => 1)).toBe(2400);
  });
});

describe("playlist helpers", () => {
  it("validates new names like the slash command", () => {
    expect(playlistNameError("  ")).toBeTruthy();
    expect(playlistNameError("a".repeat(51))).toMatch(/50/);
    expect(playlistNameError("Rock/Pop")).toMatch(/\//);
    expect(playlistNameError(" Chill ")).toBeNull();
  });
  it("matches a detail key with or without an owner, but not other views", () => {
    const withOwner = playlistDetailKey("1", "Chill Mix", false, "42");
    expect(isPlaylistDetailKey(withOwner, "1", "Chill Mix", false)).toBe(true);
    expect(isPlaylistDetailKey(playlistDetailKey("1", "Chill Mix", false, null), "1", "Chill Mix", false)).toBe(true);
    expect(isPlaylistDetailKey("/api/guild/1/playlists/Chill%20Mix/collabs?global_mode=false", "1", "Chill Mix", false)).toBe(false);
    expect(isPlaylistDetailKey(withOwner, "1", "Chill Mix", true)).toBe(false);
  });
});

describe("queue messages", () => {
  it("explains duplicates", () => {
    expect(duplicateMessage({ ok: true, added: 0, duplicate: true, playing: true }, "X")).toBe('"X" is playing now');
    expect(duplicateMessage({ ok: true, added: 0, duplicate: true, position: 3 }, "X")).toBe('"X" is already in the queue (#3)');
  });
  it("summarises bulk adds", () => {
    expect(bulkAddMessage({ ok: true, added: 4, skipped: 1 }, 5)).toBe("Added 4 songs (1 already in the queue)");
    expect(bulkAddMessage({ ok: true, added: 0, in_background: true, total: 100, capped: true }, 100))
      .toMatch(/^Adding 100 songs \(Spotify only shares the first 100\)/);
    expect(bulkAddMessage({ ok: true, added: 0, duplicate: true, skipped: 7 }, 7)).toBe("All 7 songs are already in the queue");
  });
});
