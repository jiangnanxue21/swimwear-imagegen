import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  /**
   * 和 `vitest.config.ts` 同一个理由,同一个目录。
   *
   * A45-batch15 把测试的缓存从 `node_modules/.vite` 搬了出来(EACCES:
   * root 装的依赖,普通用户跑测试时 `mkdir` 被挡)。**那条口子在构建侧
   * 一模一样地存在** —— `npm run dev` 与 `npm run build` 用的是本文件,
   * 而本文件当时没跟着改,于是同一台机器上换条路径还会再撞一次。
   *
   * 两份配置共用同一个 `.vite-cache/`:Vite 按 `optimizeDeps` 的入口集合
   * 分目录存,测试与构建的入口不同,不会互相顶掉。
   */
  cacheDir: '.vite-cache',
  plugins: [react()],
  resolve: {
    // `import.meta.dirname` 而不是 `__dirname`:Vite 8 的原生配置加载器
    // (`configLoader: 'native'`,下个大版本会变成默认)不提供 CJS 的 `__dirname`,
    // 现在每次 dev/build/test 都会为此打一条警告。换掉它是为了别让这条常驻警告
    // 把真正的构建警告淹掉 —— 需要 Node >= 20.11,CI 与 Dockerfile 都是 22。
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': { target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000', changeOrigin: true },
      '/files': { target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
})
