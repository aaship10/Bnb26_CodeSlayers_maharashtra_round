/**
 * Validate a "where to go after sign-in" value. Only same-origin absolute paths
 * are allowed: "/events/x" yes; "//evil.com", "/\evil.com", "https://evil.com",
 * "javascript:..." no. Prevents turning the sign-in redirect into an open redirect.
 */
export function safeInternalPath(value: unknown, fallback = '/'): string {
  if (typeof value !== 'string') return fallback;
  if (!value.startsWith('/')) return fallback;
  if (value.startsWith('//') || value.startsWith('/\\')) return fallback;
  if (/[\u0000-\u001f]/.test(value)) return fallback;
  return value;
}
