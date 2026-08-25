import { describe, expect, it } from "vitest";

import {
  classifySheetGesture,
  sheetStateAfterGesture,
  sheetStateAfterGripTap,
  sheetSwipeThreshold,
} from "./sheetGesture";

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

describe("mobile control sheet states", () => {
  it("expands one stage at a time", () => {
    expect(sheetStateAfterGesture("minimized", "expand")).toBe("core");
    expect(sheetStateAfterGesture("core", "expand")).toBe("full");
    expect(sheetStateAfterGesture("full", "expand")).toBe("full");
  });

  it("collapses one stage at a time", () => {
    expect(sheetStateAfterGesture("full", "collapse")).toBe("core");
    expect(sheetStateAfterGesture("core", "collapse")).toBe("minimized");
    expect(sheetStateAfterGesture("minimized", "collapse")).toBe("minimized");
  });

  it("grip taps minimize and restore the last expanded state", () => {
    expect(sheetStateAfterGripTap("core", "core")).toBe("minimized");
    expect(sheetStateAfterGripTap("full", "full")).toBe("minimized");
    expect(sheetStateAfterGripTap("minimized", "core")).toBe("core");
    expect(sheetStateAfterGripTap("minimized", "full")).toBe("full");
  });
});
