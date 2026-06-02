import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // 5173은 동일 장비의 SquadOne_AI가 사용 중 → 5180 사용.
  server: { port: 5180, strictPort: true },
});
