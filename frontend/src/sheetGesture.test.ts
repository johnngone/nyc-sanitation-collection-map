import { describe, expect, it } from "vitest";

import { classifySheetGesture, sheetSwipeThreshold } from "./sheetGesture";

describe("mobile control sheet gestures", () => {
  it("collapses after a downward swipe reaches the threshold", () => {
    expect(classifySheetGesture(100, 100 + sheetSwipeThreshold)).toBe("collapse");
  });

  it("expands after an upward swipe reaches the threshold", () => {
    expect(classifySheetGesture(100, 100 - sheetSwipeThreshold)).toBe("expand");
  });

  it("ignores shorter movement in either direction", () => {
    expect(classifySheetGesture(100, 100 + sheetSwipeThreshold - 1)).toBeNull();
    expect(classifySheetGesture(100, 100 - sheetSwipeThreshold + 1)).toBeNull();
  });
});
