const API_BASE = '/api'

export async function listDenials(): Promise<unknown> {
  const response = await fetch(`${API_BASE}/api/security/denials`)
  return response.json()
}
