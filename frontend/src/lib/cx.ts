export type ClassValue = string | false | null | undefined;

/** Join class names, skipping falsy values. */
export function cx(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(' ');
}
