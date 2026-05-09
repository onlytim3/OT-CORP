import { test, expect } from "@playwright/test";

// Catches the entire class of "content silently exceeds viewport on phones".
// Previously the dashboard rendered a 1020 px-wide table and ~48 px hero
// numbers on a 360 px screen, with no horizontal scrollbar to indicate.
test("Overview has no horizontal overflow at 360x640", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await page.goto("/");
  await page.waitForLoadState("networkidle");

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
});
