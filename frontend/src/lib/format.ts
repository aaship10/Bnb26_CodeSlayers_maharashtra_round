const dateTime = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  timeZoneName: 'short',
});

/** "Sun, 1 Nov, 15:30 – 16:00 GMT+5:30": one date, a range of times, one zone. */
export function formatWindow(openIso: string, closeIso: string): string {
  return dateTime.formatRange(new Date(openIso), new Date(closeIso));
}

const timeOnly = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  timeZoneName: 'short',
});

/** "Sat, 1 Nov, 15:30 GMT+5:30" in the viewer's locale and time zone. */
export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}

export function formatTime(iso: string): string {
  return timeOnly.format(new Date(iso));
}

const number = new Intl.NumberFormat();
export function formatCount(n: number): string {
  return number.format(n);
}
