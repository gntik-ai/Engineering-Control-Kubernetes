import { useEffect, useState } from 'react'
import { PlanTable } from '@/components/plans/PlanTable'
import { createPlan, listPlans, listQuotaDimensions, type Plan } from '@/services/plansApi'

export function PlansPage() {
  const [plans, setPlans] = useState<Plan[]>([])
  useEffect(() => {
    void listPlans().then((result) => setPlans(result.items))
    void listQuotaDimensions('?status=active')
  }, [])
  return (
    <section>
      <button onClick={() => void createPlan({ id: 'new' })}>Crear</button>
      <PlanTable plans={plans} />
    </section>
  )
}
