import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Polling keeps Vite reliable on Windows when large WASM OCR assets are
    // present in public/ocr and native file watchers report EBUSY.
    watch: {
      usePolling: true,
      interval: 250,
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
