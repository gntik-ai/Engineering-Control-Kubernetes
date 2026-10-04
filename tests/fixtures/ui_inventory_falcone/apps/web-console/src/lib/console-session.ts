import { requestJson } from './http'

export async function requestConsoleSessionJson<T>(url: string, options: { method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'; body?: unknown } = {}): Promise<T> {
  return requestJson<T>(url, options)
}
