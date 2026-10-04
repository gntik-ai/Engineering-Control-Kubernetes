import { test } from '@playwright/test'

test('plan detail', async ({ page }) => {
  await page.goto('/console/plans/starter')
})
