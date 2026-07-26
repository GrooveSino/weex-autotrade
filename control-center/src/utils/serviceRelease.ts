export function serviceReleaseLabel(releaseId: string | null): string {
  const match = releaseId?.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/)
  if (!match) return ''
  const [, year, month, day, hour, minute] = match
  return ` · 服务更新 ${year}-${month}-${day} ${hour}:${minute}`
}
