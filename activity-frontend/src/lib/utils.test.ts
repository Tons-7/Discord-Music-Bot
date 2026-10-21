import { describe, expect, it } from "vitest";
import { formatDuration, formatListenTime, joinDetails, requesterName } from "./utils";
import { toTheme, DEFAULT_THEME } from "./themes";

describe("formatDuration", () => {
  it("formats minutes and seconds", () => expect(formatDuration(83)).toBe("1:23"));
  it("adds hours when needed", () => expect(formatDuration(3725)).toBe("1:02:05"));
  it("floors fractions", () => expect(formatDuration(59.9)).toBe("0:59"));
  it("treats 0, negatives, NaN and Infinity as 0:00", () => {
    for (const v of [0, -5, NaN, Infinity]) expect(formatDuration(v)).toBe("0:00");
  });
});

describe("formatListenTime", () => {
  it("shows minutes", () => expect(formatListenTime(150)).toBe("2m"));
  it("shows hours and minutes", () => expect(formatListenTime(3 * 3600 + 120)).toBe("3h 2m"));
  it("handles nothing", () => expect(formatListenTime(0)).toBe("0m"));
});

describe("joinDetails", () => {
  it("joins only the parts that are present", () => {
    expect(joinDetails("Artist", "", null, "Requested by Sam")).toBe("Artist · Requested by Sam");
    expect(joinDetails(undefined, false)).toBe("");
  });
});

describe("requesterName", () => {
  it("drops a raw mention but keeps a name", () => {
    expect(requesterName("Sam")).toBe("Sam");
    expect(requesterName("<@123> Sam")).toBe("Sam");
    expect(requesterName("<@!123>")).toBe("<@!123>");
  });
  it("hides Unknown", () => expect(requesterName("Unknown")).toBe(""));
});

describe("toTheme", () => {
  it("accepts a known theme", () => expect(toTheme("ocean")).toBe("ocean"));
  it("falls back to the default", () => {
    expect(toTheme("nope")).toBe(DEFAULT_THEME);
    expect(toTheme(null)).toBe("midnight");
  });
});
