import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
export default defineConfig({
    plugins: [react()],
    build: {
        rollupOptions: {
            input: {
                admin: fileURLToPath(new URL("index.html", import.meta.url)),
                cabinet: fileURLToPath(new URL("cabinet/index.html", import.meta.url)),
            },
        },
    },
    server: {
        port: 5173,
        proxy: {
            "/api": {
                target: "http://localhost:8000",
                changeOrigin: true,
            },
        },
    },
});
