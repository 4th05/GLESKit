// Internal static UI build only: no runtime/profile copy and no publication.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const app = fileURLToPath(new URL("../", import.meta.url));
const isolated = mkdtempSync(path.join(tmpdir(), "gleskit-web-build-"));
const environment = {
  PATH: [path.dirname(process.execPath), "/usr/bin", "/bin", "/usr/sbin", "/sbin"].join(path.delimiter),
  LANG: "en_US.UTF-8", LC_ALL: "en_US.UTF-8", GLESKIT_ISOLATED_BUILD_DIR: isolated,
};
try {
  const result = spawnSync(process.execPath, [path.join(app, "node_modules/vite/bin/vite.js"), "build", "--mode", "local-web"],
    { cwd: app, env: environment, stdio: "inherit" });
  process.exitCode = result.status ?? 1;
} finally {
  // Only this script's newly created, empty configuration directory is removed.
  rmSync(isolated, { recursive: true });
}
