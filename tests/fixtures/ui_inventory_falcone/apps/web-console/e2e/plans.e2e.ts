import { test } from '@playwright/test'

test('plans list', async ({ page }) => {
  await page.goto('/console/plans?status=active')
})
