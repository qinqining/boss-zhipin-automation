import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 17890,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        // 后端默认端口见根目录 start.bat / backend/app/main.py
        target: 'http://localhost:27421',
        changeOrigin: true,
      },
    },
  },
})
