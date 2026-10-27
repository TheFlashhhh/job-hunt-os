/**
 * Runs the backend's OpenAPI export with the repository's own interpreter.
 *
 * `npm run gen:api` used to call a bare `python`, which on this machine is a
 * system Python with no FastAPI installed — so regenerating the API types
 * failed with ModuleNotFoundError, and the tempting fix is to hand-write the
 * response shape, which is exactly the backend/frontend drift the generated
 * client exists to prevent.
 *
 * So: prefer the repo venv, on either platform, and fall back to whatever
 * `python` resolves to if there is no venv (CI, a global install).
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

const candidates = [
  join(repoRoot, ".venv", "Scripts", "python.exe"), // Windows
  join(repoRoot, ".venv", "bin", "python"), // macOS / Linux
];

const interpreter = candidates.find((path) => existsSync(path)) ?? "python";

const result = spawnSync(interpreter, [join(repoRoot, "scripts", "export_openapi.py")], {
  stdio: "inherit",
  cwd: repoRoot,
});

if (result.error) {
  console.error(`Could not run ${interpreter}: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
