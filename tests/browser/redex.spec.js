const { test, expect } = require("./fixtures");


async function postControl(controlUrl, path, payload) {
  const response = await fetch(`${controlUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Control request failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}


async function openRedex(page, redexUrl) {
  await page.goto(redexUrl, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#sessionList")).toBeVisible();
}


test.beforeEach(async ({ controlUrl }) => {
  await postControl(controlUrl, "/reset", {});
});


test("loads sessions and selects the most recent thread", async ({ page, redexUrl }) => {
  await openRedex(page, redexUrl);

  await expect(page.locator("#sessionList")).toContainText("Primary thread");
  await expect(page.locator("#sessionList")).toContainText("Fresh thread");
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await expect(page.locator("#conversation")).toContainText("Answer 69");
});


test("rapid thread switching settles on the last click", async ({ page, redexUrl }) => {
  await openRedex(page, redexUrl);

  const background = page.locator('[data-session-id="thread-2"]');
  const primary = page.locator('[data-session-id="thread-1"]');

  await background.click();
  await primary.click();

  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await expect(page.locator("#conversation")).toContainText("Answer 69");
});


test("late session loads do not overwrite the newest thread selection", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);

  await postControl(controlUrl, "/delay", { method: "thread/read", threadId: "thread-2", seconds: 0.5 });
  await postControl(controlUrl, "/delay", { method: "thread/turns/list", threadId: "thread-2", seconds: 0.5 });

  const background = page.locator('[data-session-id="thread-2"]');
  const primary = page.locator('[data-session-id="thread-1"]');

  await background.click();
  await primary.click();

  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await expect(page.locator("#conversation")).toContainText("Answer 69");
  await page.waitForTimeout(800);
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await expect(page.locator("#conversation")).toContainText("Answer 69");
});


test("switching sessions shows a loading state instead of the previous transcript", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await expect(page.locator("#conversation")).toContainText("Answer 69");

  await postControl(controlUrl, "/delay", { method: "thread/read", threadId: "thread-2", seconds: 0.5 });
  await postControl(controlUrl, "/delay", { method: "thread/turns/list", threadId: "thread-2", seconds: 0.5 });

  await page.locator('[data-session-id="thread-2"]').click();

  await expect(page.locator("#sessionTitle")).toHaveText("Background thread");
  await expect(page.locator("#conversation")).toContainText("Loading transcript...");
  await expect(page.locator("#conversation")).toContainText("Fetching the latest turn history for this session.");
  await expect(page.locator("#conversation")).not.toContainText("Answer 69");
  await expect(page.locator("#promptInput")).toBeDisabled();

  await expect(page.locator("#conversation")).toContainText("background ready");
});


test("client timing metrics are reported back to Redex", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);
  await postControl(controlUrl, "/delay", { method: "thread/read", threadId: "thread-2", seconds: 0.35 });
  await postControl(controlUrl, "/delay", { method: "thread/turns/list", threadId: "thread-2", seconds: 0.35 });

  await page.locator('[data-session-id="thread-2"]').click();
  await expect(page.locator("#conversation")).toContainText("background ready");
  await page.evaluate(() => window.__redexMetrics?.flushUploads?.());

  await expect.poll(async () => {
    const response = await fetch(`${redexUrl}/api/client-metrics?limit=30`);
    const payload = await response.json();
    return (payload.metrics || []).map((entry) => entry.kind);
  }).toEqual(expect.arrayContaining([
    "session-switch-start",
    "session-switch-loading-painted",
    "session-detail-response",
    "session-switch-complete",
  ]));

  await expect.poll(async () => {
    const response = await fetch(`${redexUrl}/api/metrics/summary?limit=50`);
    const payload = await response.json();
    return payload?.client?.byKind?.["session-switch-complete"]?.numeric?.totalMs?.count || 0;
  }).toBeGreaterThan(0);

  await expect.poll(async () => {
    const response = await fetch(`${redexUrl}/api/metrics/summary?limit=50`);
    const payload = await response.json();
    return payload?.server?.byKind?.["session-detail"]?.numeric?.totalMs?.count || 0;
  }).toBeGreaterThan(0);
});


test("background updates do not steal focus and mark the thread unseen", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);

  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await postControl(controlUrl, "/prompt", { threadId: "thread-2", text: "background ping" });

  const background = page.locator('[data-session-id="thread-2"]');
  await expect(background).toHaveClass(/unseen/);
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");

  await background.click();
  await expect(page.locator("#sessionTitle")).toHaveText("Background thread");
  await expect(page.locator("#conversation")).toContainText("Background final answer.");
});


test("active thread shows streamed text before the final answer lands", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);
  await page.locator('[data-session-id="thread-1"]').click();
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");
  await page.evaluate(() => window.__redexMetrics?.clear?.());

  await postControl(controlUrl, "/stream", {
    threadId: "thread-1",
    promptText: "stream this",
    deltas: ["Streaming ", "preview"],
    finalText: "Stream finished cleanly.",
    phase: "final_answer",
    delaySeconds: 0.12,
  });

  await page.waitForFunction(
    () => window.__redexMetrics?.latestFirstStreamPaintLatency?.() != null,
    null,
    { timeout: 5000, polling: 20 },
  );
  await expect(page.locator("#conversation")).toContainText("stream this");
  await expect(page.locator("#conversation")).toContainText("Stream finished cleanly.");
  const metrics = await page.evaluate(() => ({
    first: window.__redexMetrics?.latestFirstStreamPaintLatency?.(),
    completion: window.__redexMetrics?.latestCompletionPaintLatency?.(),
  }));
  expect(metrics.first).not.toBeNull();
  expect(metrics.completion).not.toBeNull();
  const userTurns = page.locator("#conversation article.user");
  await expect(userTurns.filter({ hasText: "stream this" })).toHaveCount(1);

  const assistantTail = await page.evaluate(() => {
    const articles = Array.from(document.querySelectorAll("#conversation article"));
    const assistant = [...articles].reverse().find((node) => node.className.includes("assistant"));
    return assistant ? { cls: assistant.className, text: assistant.innerText } : null;
  });
  expect(assistantTail?.cls).toContain("assistant");
  expect(assistantTail?.text).toContain("Stream finished cleanly.");
});


test("sending a prompt echoes it immediately with a pending state", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);
  await page.locator('[data-session-id="thread-1"]').click();
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");

  await postControl(controlUrl, "/delay", { method: "turn/start", threadId: "thread-1", seconds: 0.45 });

  await page.locator("#promptInput").fill("optimistic hello");
  await page.locator("#sendButton").click();

  const pendingBubble = page.locator("#conversation article.user.pending").last();
  await expect(pendingBubble).toContainText("optimistic hello");
  await expect(pendingBubble).toContainText("sending...");
  await expect(page.locator("#promptInput")).toHaveValue("");
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");

  await expect(page.locator("#conversation")).toContainText("Active thread final answer.");
  await expect(page.locator("#conversation article.user.pending")).toHaveCount(0);
});


test("scrolling up keeps position stable and shows the jump-to-end control", async ({ page, redexUrl, controlUrl }) => {
  await openRedex(page, redexUrl);
  await page.locator('[data-session-id="thread-1"]').click();
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");

  const conversation = page.locator("#conversation");
  await conversation.hover();
  await page.mouse.wheel(0, -4000);
  await expect
    .poll(async () =>
      conversation.evaluate((element) => ({
        scrollTop: element.scrollTop,
        distanceFromEnd: element.scrollHeight - element.clientHeight - element.scrollTop,
      })),
    )
    .toMatchObject({ distanceFromEnd: expect.any(Number) });

  await expect
    .poll(async () => conversation.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop))
    .toBeGreaterThan(56);
  await expect(page.locator("#scrollToEndButton")).toHaveClass(/visible/);
  const before = await conversation.evaluate((element) => element.scrollTop);

  await postControl(controlUrl, "/prompt", { threadId: "thread-1", text: "active ping" });
  await expect(page.locator("#conversation")).toContainText("Active thread final answer.");

  const after = await conversation.evaluate((element) => element.scrollTop);
  expect(after).toBeLessThanOrEqual(before + 24);
  await expect(page.locator("#scrollToEndButton")).toHaveClass(/visible/);
});


test("loading older history prepends messages without losing the reader position", async ({ page, redexUrl }) => {
  await openRedex(page, redexUrl);
  await page.locator('[data-session-id="thread-1"]').click();
  await expect(page.locator("#sessionTitle")).toHaveText("Primary thread");

  const conversation = page.locator("#conversation");
  await expect(page.locator("#loadOlderButton")).toBeVisible();
  await conversation.hover();
  await page.mouse.wheel(0, -3000);
  await expect
    .poll(async () => conversation.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop))
    .toBeGreaterThan(56);

  const before = await conversation.evaluate((element) => element.scrollTop);
  await page.locator("#loadOlderButton").click();
  await expect(page.locator("#conversation")).toContainText("Answer 10");
  const after = await conversation.evaluate((element) => element.scrollTop);

  expect(after).toBeGreaterThan(before);
});
