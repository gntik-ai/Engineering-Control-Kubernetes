import { requestJson } from '@/lib/http'
import { requestConsoleSessionJson } from '@/lib/console-session'

export interface Plan {
  id: string
}

// A URL builder, composed by the callers below (as falcone's flowsBase is).
const plansBase = () => `/v1/plans`

export function listPlans() {
  return requestConsoleSessionJson<{ items: Plan[] }>(plansBase())
}

export function getPlan(planId: string) {
  return requestConsoleSessionJson<Plan>(`${plansBase()}/${encodeURIComponent(planId)}`)
}

export function createPlan(body: Plan) {
  return requestConsoleSessionJson<Plan>('/v1/plans', { method: 'POST', body })
}

export function deletePlan(planId: string) {
  return requestConsoleSessionJson<void>(`/v1/plans/${planId}`, {
    method: 'DELETE'
  })
}

export function listQuotaDimensions(query: string) {
  const url = `/v1/quota-dimensions${query}`
  return requestJson<{ items: string[] }>(url)
}
