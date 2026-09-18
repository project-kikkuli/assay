import type { ItemUpdate } from "./src/client/types.gen";
import { chromium, expect } from "@playwright/test";

const fixture = JSON.parse(process.env.ASSAY_BOUNDARY_FIXTURE!);
const base = process.env.ASSAY_API!;
const headers = { Authorization: `Bearer ${fixture.token}`, "Content-Type": "application/json" };

// This value is accepted by the actual generated TypeScript client type.
const update: ItemUpdate = { title: null };
const invalidUpdate = await fetch(`${base}/api/v1/items/${fixture.item}`, {
  method: "PUT", headers, body: JSON.stringify(update),
});
const afterUpdate = await fetch(`${base}/api/v1/items/${fixture.item}`, { headers });
const persisted = await afterUpdate.json();
const firstPage = await fetch(`${base}/api/v1/items/?skip=0&limit=100`, { headers });
const first = await firstPage.json();
const remainder = await fetch(`${base}/api/v1/items/?skip=100&limit=100`, { headers });
const rest = await remainder.json();
const negativeOffset = await fetch(`${base}/api/v1/items/?skip=-1&limit=100`, { headers });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
  await page.goto(`${base}/login`);
  await page.evaluate(token => localStorage.setItem("access_token", token), fixture.token);
  await page.goto(`${base}/items`);
  await expect(page.locator("tbody tr").first().locator("td").first()).toContainText(/[0-9a-f]{8}-[0-9a-f]{4}-/);
  const titles = new Set<string>();
  const observedPages: { count: number; first: string; last: string }[] = [];
  let pages = 0;
  while (true) {
    const rows = await page.locator("tbody tr").evaluateAll(rows =>
      rows.map(row => row.children[1]?.textContent?.trim() ?? ""));
    for (const title of rows) titles.add(title);
    observedPages.push({ count: rows.length, first: rows[0], last: rows.at(-1)! });
    pages++;
    if (pages > 30) throw new Error("pagination did not terminate within fixture bound");
    const next = page.getByRole("button", { name: "Go to next page", exact: true });
    if (await next.isDisabled()) break;
    await next.click();
    await expect(page.locator("tbody tr").first().locator("td").nth(1)).not.toHaveText(rows[0]);
  }
  const injected = await page.evaluate(() => Boolean((globalThis as { __assayInjected?: boolean }).__assayInjected));
  if (process.env.ASSAY_SCREENSHOT) await page.screenshot({ path: process.env.ASSAY_SCREENSHOT, fullPage: true });
  console.log(JSON.stringify({
    null_update: { generated_type_accepts: true, http_status: invalidUpdate.status,
      title_preserved: persisted.title === "fixture-000", expected_status_class: "4xx" },
    negative_offset: { http_status: negativeOffset.status, expected_status_class: "4xx" },
    pagination: { seeded: fixture.seeded_count, api_total: first.count, api_first_page: first.data.length,
      api_second_page: rest.data.length, browser_pages: pages, browser_unique_titles: titles.size,
      all_items_reachable: titles.size === fixture.seeded_count, observed_pages: observedPages },
    title_rendering: { injected_script_executed: injected, safely_rendered: !injected },
  }));
} finally {
  await browser.close();
}
