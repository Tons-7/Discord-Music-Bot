import { describe, expect, it } from "vitest";
import {
  clampSeek, effectLabel, extrapolatePosition, playbackRate, presenceTimestamps, relativeSeekTarget,
} from "./playback";

describe("clampSeek", () => {
  it("stops 5s short of the end", () => expect(clampSeek(300, 240)).toBe(235));
  it("never goes below 0", () => expect(clampSeek(-3, 240)).toBe(0));
  it("only clamps at 0 when the length is unknown", () => expect(clampSeek(999, 0)).toBe(999));
});

describe("relativeSeekTarget", () => {
  it("steps forward and back", () => {
    expect(relativeSeekTarget(60.7, 5, 240)).toBe(65);
    expect(relativeSeekTarget(60.7, -5, 240)).toBe(55);
  });
  it("does not jump backward when seeking forward near the end", () => {
    expect(relativeSeekTarget(238, 5, 240)).toBeNull();
    expect(relativeSeekTarget(235, 5, 240)).toBeNull();
  });
  it("stops at the clamp when it is still ahead", () => expect(relativeSeekTarget(233, 5, 240)).toBe(235));
  it("does nothing seeking back at the start", () => expect(relativeSeekTarget(0, -5, 240)).toBeNull());
});

describe("extrapolatePosition", () => {
  const tick = { position: 10, is_paused: false, at: 1000 };
  it("advances by the time since the tick at the playback rate", () => {
    expect(extrapolatePosition(tick, 2000, 1)).toBe(11);
    expect(extrapolatePosition(tick, 2000, 1.5)).toBe(11.5);
  });
  it("holds while paused", () => expect(extrapolatePosition({ ...tick, is_paused: true }, 5000, 1)).toBe(10));
  it("holds before any tick arrived", () => expect(extrapolatePosition({ ...tick, at: 0 }, 5000, 1)).toBe(10));
  it("never passes the end", () => expect(extrapolatePosition(tick, 100_000, 1, 12)).toBe(12));
});

describe("playbackRate", () => {
  it("multiplies speed by the effect", () => {
    expect(playbackRate(1, "nightcore")).toBe(1.25);
    expect(playbackRate(2, "vaporwave")).toBeCloseTo(1.6);
    expect(playbackRate(1.5, "bass_boost")).toBe(1.5);
  });
  it("treats a bad speed as 1x", () => expect(playbackRate(0, "none")).toBe(1));
});

describe("presenceTimestamps", () => {
  it("is plain at 1x", () => {
    expect(presenceTimestamps(100_000, 30, 200, 1)).toEqual({ start: 70, end: 270 });
  });
  it("scales elapsed and remaining time by the rate", () => {
    expect(presenceTimestamps(100_000, 30, 200, 2)).toEqual({ start: 85, end: 185 });
  });
});

describe("effectLabel", () => {
  it("uses the bot's names", () => {
    expect(effectLabel("nightcore")).toBe("Nightcore");
    expect(effectLabel("8d")).toBe("8D Audio");
  });
});
