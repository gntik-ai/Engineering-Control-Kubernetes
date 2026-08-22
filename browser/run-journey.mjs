import { chromium, firefox } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const allowedBrowsers = new Map([
  ["chromium", chromium],
  ["firefox", firefox],
]);
const allowedActions = new Set(["click", "fill", "expect-url", "wait-for-text"]);

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const planPath = required("ENGINEERING_JOURNEY_PLAN");
const outputRoot = path.resolve(required("ENGINEERING_JOURNEY_OUTPUT"));
const plan = JSON.parse(await fs.readFile(planPath, "utf8"));
if (!Array.isArray(plan.allowed_origins) || !plan.allowed_origins.length) {
  throw new Error("journey plan requires allowed_origins");
}
if (!Array.isArray(plan.steps) || plan.steps.length > 100) {
  throw new Error("journey plan requires at most 100 bounded steps");
}
const origins = new Set(plan.allowed_origins.map((value) => new URL(value).origin));
const target = new URL(plan.start_url);
if (!origins.has(target.origin)) throw new Error("start_url is outside the journey allowlist");
for (const step of plan.steps) {
  if (!allowedActions.has(step.action)) throw new Error("unsupported journey action");
  if (step.action === "fill") {
    if (
      typeof step.value_from_env !== "string" ||
      !/^JOURNEY_[A-Z0-9_]+$/.test(step.value_from_env)
    ) {
      throw new Error("fill actions require a JOURNEY_* credential environment reference");
    }
    if (Object.hasOwn(step, "value")) throw new Error("literal fill values are forbidden");
  }
}
const usesCredentials = plan.steps.some((step) => step.action === "fill");
const captureVisualEvidence = !usesCredentials && plan.capture_visual_evidence !== false;
await fs.mkdir(outputRoot, { recursive: true, mode: 0o700 });

const browserType = allowedBrowsers.get(plan.browser || "chromium");
if (!browserType) throw new Error("unsupported browser");
const browser = await browserType.launch({ headless: true });
const context = await browser.newContext({
  ignoreHTTPSErrors: false,
  serviceWorkers: "block",
});
await context.route("**/*", async (route) => {
  let origin;
  try {
    origin = new URL(route.request().url()).origin;
  } catch {
    await route.abort("blockedbyclient");
    return;
  }
  if (origins.has(origin)) await route.continue();
  else await route.abort("blockedbyclient");
});
if (captureVisualEvidence) {
  await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
}
const page = await context.newPage();
const consoleEvents = [];
const networkEvents = [];
page.on("console", (message) => {
  if (consoleEvents.length < 5000) {
    consoleEvents.push({ type: message.type(), text: message.text().slice(0, 4000) });
  }
});
page.on("response", (response) => {
  if (networkEvents.length < 5000) {
    const url = new URL(response.url());
    networkEvents.push({ origin: url.origin, path: url.pathname, status: response.status() });
  }
});

let status = "PASS";
let error = null;
try {
  await page.goto(plan.start_url, {
    waitUntil: "networkidle",
    timeout: Math.min(Math.max(plan.timeout_ms || 30000, 1000), 120000),
  });
  for (const step of plan.steps) {
    if (step.action === "click") {
      await page.getByRole(step.role, { name: step.name }).click();
    } else if (step.action === "fill") {
      await page.getByLabel(step.label).fill(required(step.value_from_env));
    } else if (step.action === "expect-url") {
      const current = new URL(page.url());
      if (!origins.has(current.origin) || !current.pathname.includes(step.path_contains)) {
        throw new Error("URL expectation failed");
      }
    } else if (step.action === "wait-for-text") {
      await page.getByText(step.text, { exact: Boolean(step.exact) }).waitFor();
    }
  }
  if (captureVisualEvidence) {
    await page.screenshot({ path: path.join(outputRoot, "final.png"), fullPage: true });
  }
} catch (caught) {
  status = "FAIL";
  error = caught instanceof Error ? caught.message : "journey failed";
  if (captureVisualEvidence) {
    await page.screenshot({ path: path.join(outputRoot, "failure.png"), fullPage: true });
  }
} finally {
  if (captureVisualEvidence) {
    await context.tracing.stop({ path: path.join(outputRoot, "trace.zip") });
  }
  await context.close();
  await browser.close();
}

await fs.writeFile(
  path.join(outputRoot, "result.json"),
  JSON.stringify(
    {
      status,
      error,
      console: consoleEvents,
      network: networkEvents,
      visual_evidence: captureVisualEvidence ? "CAPTURED" : "DISABLED_CREDENTIAL_SAFETY",
    },
    null,
    2,
  ),
  { mode: 0o600 },
);
if (status !== "PASS") process.exitCode = 1;
