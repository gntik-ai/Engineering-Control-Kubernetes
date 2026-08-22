import { chromium, firefox } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const allowedBrowsers = new Map([
  ["chromium", chromium],
  ["firefox", firefox],
]);

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const planPath = required("ENGINEERING_JOURNEY_PLAN");
const outputRoot = path.resolve(required("ENGINEERING_JOURNEY_OUTPUT"));
const credentialPath = process.env.ENGINEERING_JOURNEY_CREDENTIAL_FILE;
const plan = JSON.parse(await fs.readFile(planPath, "utf8"));
if (!Array.isArray(plan.allowed_origins) || !plan.allowed_origins.length) {
  throw new Error("journey plan requires allowed_origins");
}
const target = new URL(plan.start_url);
if (!plan.allowed_origins.includes(target.origin)) {
  throw new Error("start_url is outside the journey allowlist");
}
if (credentialPath) {
  await fs.access(credentialPath);
}
await fs.mkdir(outputRoot, { recursive: true, mode: 0o750 });

const browserType = allowedBrowsers.get(plan.browser || "chromium");
if (!browserType) throw new Error("unsupported browser");
const browser = await browserType.launch({ headless: true });
const context = await browser.newContext({
  ignoreHTTPSErrors: false,
  serviceWorkers: "block",
});
await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
const page = await context.newPage();
const consoleEvents = [];
const networkEvents = [];
page.on("console", (message) => consoleEvents.push({ type: message.type(), text: message.text() }));
page.on("response", (response) => {
  const url = new URL(response.url());
  networkEvents.push({ origin: url.origin, path: url.pathname, status: response.status() });
});

let status = "PASS";
let error = null;
try {
  await page.goto(plan.start_url, { waitUntil: "networkidle", timeout: plan.timeout_ms || 30000 });
  for (const step of plan.steps || []) {
    if (step.action === "click") await page.getByRole(step.role, { name: step.name }).click();
    else if (step.action === "fill") await page.getByLabel(step.label).fill(step.value_from_env ? required(step.value_from_env) : step.value);
    else if (step.action === "expect-url") {
      if (!page.url().includes(step.contains)) throw new Error("URL expectation failed");
    } else throw new Error(`unsupported journey action: ${step.action}`);
  }
  await page.screenshot({ path: path.join(outputRoot, "final.png"), fullPage: true });
} catch (caught) {
  status = "FAIL";
  error = caught instanceof Error ? caught.message : "journey failed";
  await page.screenshot({ path: path.join(outputRoot, "failure.png"), fullPage: true });
} finally {
  await context.tracing.stop({ path: path.join(outputRoot, "trace.zip") });
  await context.close();
  await browser.close();
}

await fs.writeFile(
  path.join(outputRoot, "result.json"),
  JSON.stringify({ status, error, console: consoleEvents, network: networkEvents }, null, 2),
  { mode: 0o640 },
);
if (status !== "PASS") process.exitCode = 1;
