import { deletePlan, getPlan } from '@/services/plansApi'

export function PlanDetailPage() {
  void getPlan('starter')
  return <button onClick={() => void deletePlan('starter')}>Eliminar</button>
}
