import type { Plan } from '@/services/plansApi'

export function PlanTable({ plans }: { plans: Plan[] }) {
  return <table>{plans.map((plan) => <tr key={plan.id} />)}</table>
}
