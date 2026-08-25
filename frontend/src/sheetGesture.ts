export const sheetSwipeThreshold = 32;

export type SheetGesture = "collapse" | "expand";
export type MobileSheetState = "minimized" | "core" | "full";
export type ExpandedSheetState = Exclude<MobileSheetState, "minimized">;

export function classifySheetGesture(startY: number, endY: number): SheetGesture | null {
  const distance = endY - startY;
  if (Math.abs(distance) < sheetSwipeThreshold) return null;
  return distance > 0 ? "collapse" : "expand";
}

export function sheetStateAfterGesture(state: MobileSheetState, gesture: SheetGesture): MobileSheetState {
  if (gesture === "expand") {
    if (state === "minimized") return "core";
    return "full";
  }
  if (state === "full") return "core";
  return "minimized";
}

export function sheetStateAfterGripTap(
  state: MobileSheetState,
  lastExpandedState: ExpandedSheetState,
): MobileSheetState {
  return state === "minimized" ? lastExpandedState : "minimized";
}
