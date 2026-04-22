const { test: base, expect } = require("@playwright/test");
const { spawn } = require("node:child_process");
const readline = require("node:readline");
const path = require("node:path");


async function terminateProcess(child) {
  if (!child || child.killed) {
    return;
  }
  child.kill();
  await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(), 3000);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}


async function startBrowserEnv() {
  const repoRoot = path.resolve(__dirname, "..", "..");
  const child = spawn("python", ["tests/browser_env.py"], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });

  const rl = readline.createInterface({ input: child.stdout });
  const config = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Timed out starting browser env.\n${stderr}`)), 15_000);
    rl.once("line", (line) => {
      clearTimeout(timer);
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(new Error(`Failed to parse browser env config: ${line}\n${stderr}`));
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Browser env exited early with code ${code}.\n${stderr}`));
    });
  });
  rl.close();
  return { child, config, repoRoot };
}


exports.test = base.extend({
  browserEnv: [
    async ({}, use) => {
      const env = await startBrowserEnv();
      try {
        await use(env);
      } finally {
        await terminateProcess(env.child);
      }
    },
    { scope: "worker" },
  ],

  redexUrl: async ({ browserEnv }, use) => {
    await use(browserEnv.config.redexUrl);
  },

  controlUrl: async ({ browserEnv }, use) => {
    await use(browserEnv.config.controlUrl);
  },
});

exports.expect = expect;
exports.startBrowserEnv = startBrowserEnv;
exports.terminateProcess = terminateProcess;
