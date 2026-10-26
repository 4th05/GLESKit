import { defineConfig } from "vite";

export default defineConfig(() => {
  if (!process.env.GLESKIT_ISOLATED_BUILD_DIR) {
    throw new Error("Use npm run build:web; local browser builds require isolated inputs.");
  }
  return {
  // Browser builds must not read ignored .env files or copy a public/
  // directory containing personal files. The build wrapper supplies a fresh folder.
  envDir: process.env.GLESKIT_ISOLATED_BUILD_DIR,
  publicDir: false,
  build: { outDir: "dist-web" },
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true
  }
  };
});
