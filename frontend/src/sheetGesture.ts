export const sheetSwipeThreshold = 32;

export type SheetGesture = "collapse" | "expand";

export function classifySheetGesture(startY: number, endY: number): SheetGesture | null {
  const distance = endY - startY;
  if (Math.abs(distance) < sheetSwipeThreshold) return null;
  return distance > 0 ? "collapse" : "expand";
}
