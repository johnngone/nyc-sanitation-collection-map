export const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;
export type Weekday = (typeof weekdays)[number];

const calendarWeekdays = ["Sunday", ...weekdays] as const;
type CalendarWeekday = (typeof calendarWeekdays)[number];

const planningTimeZone = "America/New_York";
const planningCutoffHour = 16;
const planningTimeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: planningTimeZone,
  weekday: "long",
  hour: "numeric",
  hourCycle: "h23",
});

export function dayCode(day: Weekday): string {
  return day.slice(0, 3).toUpperCase();
}

export function dayFromCode(code: string | null, now = new Date()): Weekday {
  return weekdays.find((day) => dayCode(day) === code) ?? automaticCollectionDay(now);
}

export function automaticCollectionDay(now = new Date()): Weekday {
  const parts = planningTimeFormatter.formatToParts(now);
  const weekdayValue = parts.find((part) => part.type === "weekday")?.value;
  const hour = Number(parts.find((part) => part.type === "hour")?.value);

  if (!weekdayValue || !isCalendarWeekday(weekdayValue) || !Number.isInteger(hour)) {
    throw new Error("Could not determine the current New York planning day");
  }

  const dayOffset = hour >= planningCutoffHour ? 1 : 0;
  const dayIndex = (calendarWeekdays.indexOf(weekdayValue) + dayOffset) % calendarWeekdays.length;
  const planningDay = calendarWeekdays[dayIndex];

  // DSNY collection schedules in this application run Monday through Saturday.
  return planningDay === "Sunday" ? "Monday" : planningDay;
}

function isCalendarWeekday(value: string): value is CalendarWeekday {
  return (calendarWeekdays as readonly string[]).includes(value);
}
