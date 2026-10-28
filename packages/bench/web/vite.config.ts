import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

// `npm run dev` proxies the API to a running `bg3bench-serve` (default port 8790). changeOrigin: the panel answers
// only a Host of 127.0.0.1:<port> or localhost:<port>. Writes in dev need ?token=<~/.config/bg3bench/panel-token>.
export default defineConfig({
  plugins: [svelte()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8790", changeOrigin: true } } },
  build: { outDir: "dist", emptyOutDir: true },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
