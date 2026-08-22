export function resolveApiUrl(path: string, configuredBase: string, origin: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  if (/^\/\//.test(path)) return `${new URL(origin).protocol}${path}`;

  const base = configuredBase.replace(/\/+$/, "");
  const suffix = `/${path.replace(/^\/+/, "")}`;
  const combined = `${base}${suffix}`;
  if (/^https?:\/\//i.test(combined)) return combined;
  if (/^\/\//.test(combined)) return `${new URL(origin).protocol}${combined}`;
  return `${origin.replace(/\/+$/, "")}/${combined.replace(/^\/+/, "")}`;
}
