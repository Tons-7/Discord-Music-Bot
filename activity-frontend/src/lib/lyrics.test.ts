import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { activeLineIndex, OFFSET_MAX, parseLRC, readOffset, writeOffset } from "./lyrics";

describe("parseLRC", () => {
  it("repeats a line at each of its leading timestamps", () => {
    expect(parseLRC("[00:01.50][00:10.00]chorus")).toEqual([
      { time: 1.5, text: "chorus" },
      { time: 10, text: "chorus" },
    ]);
  });

  it("reads 2- and 3-digit fractions and ':' separators", () => {
    const lines = parseLRC("[01:02.5]a\n[01:03.250]b\n[01:04:75]c\n[01:05]d");
    expect(lines.map(l => l.time)).toEqual([62.5, 63.25, 64.75, 65]);
  });

  it("sorts by time", () => {
    expect(parseLRC("[00:20.00]second\n[00:05.00]first").map(l => l.text)).toEqual(["first", "second"]);
  });

  it("ignores metadata and untimed lines", () => {
    expect(parseLRC("[ar:Someone]\nplain text\n[00:01.00]sung")).toEqual([{ time: 1, text: "sung" }]);
  });

  it("keeps a bare timestamp after a sung line as a break", () => {
    expect(parseLRC("[00:01.00]sung\n[00:30.00] \n[00:45.00]again")).toEqual([
      { time: 1, text: "sung" },
      { time: 30, text: "" },
      { time: 45, text: "again" },
    ]);
  });

  it("drops leading and repeated breaks", () => {
    expect(parseLRC("[00:00.00]\n[00:01.00]sung\n[00:02.00]\n[00:03.00]").map(l => l.text)).toEqual(["sung", ""]);
  });
});

describe("activeLineIndex", () => {
  const lines = parseLRC("[00:01.00]a\n[00:05.00]b\n[00:09.00]");
  it("is -1 before the first line", () => expect(activeLineIndex(lines, 0.5)).toBe(-1));
  it("follows the position", () => expect(activeLineIndex(lines, 6)).toBe(1));
  it("leaves the last sung line at a break", () => expect(activeLineIndex(lines, 10)).toBe(2));
});

describe("lyrics offsets", () => {
  let store: Record<string, string>;
  beforeEach(() => {
    store = {};
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    };
  });
  afterEach(() => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  });

  it("round-trips and clears at 0", () => {
    writeOffset("song", 1.5);
    expect(readOffset("song")).toBe(1.5);
    writeOffset("song", 0);
    expect(readOffset("song")).toBe(0);
    expect(JSON.parse(store.lyricsOffsets)).toEqual({});
  });

  it("keeps at most OFFSET_MAX songs, evicting the least recently adjusted", () => {
    for (let i = 0; i < OFFSET_MAX; i++) writeOffset(`s${i}`, 1);
    writeOffset("s0", 2); // touched again: now the most recent
    writeOffset("new", 1);
    const kept = Object.keys(JSON.parse(store.lyricsOffsets));
    expect(kept).toHaveLength(OFFSET_MAX);
    expect(kept).toContain("s0");
    expect(kept).not.toContain("s1");
    expect(readOffset("s0")).toBe(2);
  });

  it("survives unavailable storage", () => {
    delete (globalThis as { localStorage?: unknown }).localStorage;
    expect(() => writeOffset("song", 1)).not.toThrow();
    expect(readOffset("song")).toBe(0);
  });
});
