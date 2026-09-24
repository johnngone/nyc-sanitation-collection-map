import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  envDir: fileURLToPath(new URL("..", import.meta.url)),
  plugins: [react()],
});

