import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时（npm run dev）把 /api 代理到 Flask，前端直接 fetch("/api/chat")，无需 CORS。
// 后端接口现在统一挂在 /api 下，所以这里不再 rewrite 掉前缀，dev / 打包两种模式路径一致。
// 日常使用走 python app.py 托管的打包静态文件（见项目根 start_app.ps1）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:5001",
        changeOrigin: true,
        // /api/chat 要串几次 DeepSeek 调用；/api/kb/*/upload 建库可能要几分钟。别让代理提前掐断
        timeout: 600000,
        proxyTimeout: 600000,
        configure: (proxy) => {
          proxy.on("error", (err, req, res) => {
            console.error(`[proxy] ${req.method} ${req.url} ->`, err.message);
            if (res && !res.headersSent && res.writeHead) {
              res.writeHead(502, { "Content-Type": "application/json" });
              res.end(
                JSON.stringify({
                  error: `代理到后端失败: ${err.code || err.message}（后端是否在 127.0.0.1:5001 运行？）`,
                })
              );
            }
          });
        },
      },
    },
  },
});
