type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export async function requestJson<T>(url: string, options: { method?: HttpMethod; body?: unknown } = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const response = await fetch(url, { method, body: JSON.stringify(options.body) })
  return (await response.json()) as T
}
