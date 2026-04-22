const { chromium, expect } = require("@playwright/test");
const { startBrowserEnv, terminateProcess } = require("./browser/fixtures");


async function postJson(baseUrl, path, payload) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}


async function measure(name, action) {
  const started = performance.now();
  await action();
  return { name, ms: performance.now() - started };
}


async function main() {
  const env = await startBrowserEnv();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  try {
    const { redexUrl, controlUrl } = env.config;
    const results = [];

    await postJson(controlUrl, "/reset", {});

    results.push(
      await measure("initial_open", async () => {
        await page.goto(redexUrl, { waitUntil: "domcontentloaded" });
        await expect(page.locator("#sessionList")).toBeVisible();
        await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
        await expect(page.locator("#conversation")).toContainText("Answer 69");
      }),
    );

    results.push(
      await measure("switch_to_background", async () => {
        await page.locator('[data-session-id="thread-2"]').click();
        await expect(page.locator("#sessionTitle")).toHaveText("Background thread");
        await expect(page.locator("#conversation")).toContainText("background ready");
      }),
    );

    results.push(
      await measure("switch_back_cached", async () => {
        await page.locator('[data-session-id="thread-1"]').click();
        await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
        await expect(page.locator("#conversation")).toContainText("Answer 69");
      }),
    );

    results.push(
      await measure("background_unseen_marker", async () => {
        await postJson(controlUrl, "/prompt", { threadId: "thread-2", text: "perf ping" });
        await expect(page.locator('[data-session-id="thread-2"]')).toHaveClass(/unseen/);
      }),
    );

    const budgets = {
      initial_open: 1500,
      switch_to_background: 800,
      switch_back_cached: 400,
      background_unseen_marker: 1500,
    };

    for (const result of results) {
      const budget = budgets[result.name];
      const withinBudget = result.ms <= budget;
      console.log(
        `${result.name.padEnd(24)} ${result.ms.toFixed(1).padStart(7)}ms  budget=${budget}ms  ${withinBudget ? "OK" : "SLOW"}`,
      );
      if (!withinBudget) {
        process.exitCode = 1;
      }
    }
  } finally {
    await page.close().catch(() => {});
    await browser.close().catch(() => {});
    await terminateProcess(env.child);
  }
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
