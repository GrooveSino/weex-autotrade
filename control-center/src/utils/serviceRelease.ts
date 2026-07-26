export function serviceReleaseLabel(releaseId: string | null): string {
  const match = releaseId?.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/)
  if (!match) return ''
  const [, year, month, day, hour, minute] = match
  const releasedAt = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute)))
  const local = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(releasedAt).replaceAll('/', '-')
  return ` · 服务更新 ${local}`
}
