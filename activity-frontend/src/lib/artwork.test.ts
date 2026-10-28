import { describe, expect, it } from "vitest";
import { artworkSources, contentBox, frameRatio } from "./artwork";

// An RGBA image of `width`×`height` that is black except a grey rectangle.
function image(width: number, height: number, picture: { x: number; y: number; w: number; h: number }) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const inside = x >= picture.x && x < picture.x + picture.w && y >= picture.y && y < picture.y + picture.h;
      const i = (y * width + x) * 4;
      data[i] = data[i + 1] = data[i + 2] = inside ? 128 : 4;
      data[i + 3] = 255;
    }
  }
  return data;
}

describe("artworkSources", () => {
  it("tries a YouTube video's HD frame first, then the song's own thumbnail", () => {
    expect(artworkSources("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")).toEqual([
      "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
      "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    ]);
    expect(artworkSources("https://i.ytimg.com/vi_webp/dQw4w9WgXcQ/maxresdefault.webp")[0])
      .toBe("https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg");
  });
  it("keeps other artwork as it is", () => {
    expect(artworkSources("https://i1.sndcdn.com/artworks-abc-t500x500.jpg"))
      .toEqual(["https://i1.sndcdn.com/artworks-abc-t500x500.jpg"]);
    expect(artworkSources("https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"))
      .toEqual(["https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"]);
    expect(artworkSources(null)).toEqual([]);
  });
});

describe("contentBox", () => {
  it("drops the letterbox of a 4:3 YouTube thumbnail around a 16:9 video", () => {
    expect(contentBox(image(160, 120, { x: 0, y: 15, w: 160, h: 90 }), 160, 120))
      .toEqual({ x: 0, y: 15, width: 160, height: 90 });
  });
  it("finds the square album cover between pillarbox bars", () => {
    expect(contentBox(image(160, 90, { x: 35, y: 0, w: 90, h: 90 }), 160, 90))
      .toEqual({ x: 35, y: 0, width: 90, height: 90 });
  });
  it("keeps a picture without bars, and a dark picture, whole", () => {
    expect(contentBox(image(160, 90, { x: 0, y: 0, w: 160, h: 90 }), 160, 90))
      .toEqual({ x: 0, y: 0, width: 160, height: 90 });
    // Mostly black, with a bright detail off to one side: not bars.
    expect(contentBox(image(160, 90, { x: 120, y: 30, w: 20, h: 20 }), 160, 90))
      .toEqual({ x: 0, y: 0, width: 160, height: 90 });
  });
});

describe("frameRatio", () => {
  it("is the picture's shape between square and 16:9", () => {
    expect(frameRatio(1280, 720)).toBeCloseTo(16 / 9);
    expect(frameRatio(500, 500)).toBe(1);
    expect(frameRatio(4, 3)).toBeCloseTo(4 / 3);
    expect(frameRatio(720, 1280)).toBe(1);
    expect(frameRatio(3000, 1000)).toBeCloseTo(16 / 9);
    expect(frameRatio(0, 0)).toBe(1);
  });
});
