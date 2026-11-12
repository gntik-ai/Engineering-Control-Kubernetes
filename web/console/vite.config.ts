import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const require = createRequire(import.meta.url);
const THEME_BOOTSTRAP = 'assets/theme-bootstrap.js';

// Served by the dashboard service under /console (see dashboard/app.py), whose
// Content-Security-Policy allows only same-origin scripts, styles and images: the theme
// script ships as a file (never inline) and no asset is inlined as a data: URI.
export default defineConfig({
  base: '/console/',
  plugins: [
    react(),
    tailwindcss(),
    {
      name: 'gntik-theme-bootstrap',
      generateBundle() {
        this.emitFile({
          type: 'asset',
          fileName: THEME_BOOTSTRAP,
          source: readFileSync(require.resolve('@gntik-ai/ui/theme-bootstrap.js'), 'utf8'),
        });
      },
      transformIndexHtml: () => [
        { tag: 'script', attrs: { src: `/console/${THEME_BOOTSTRAP}` }, injectTo: 'head-prepend' },
      ],
    },
  ],
  resolve: { dedupe: ['react', 'react-dom'] },
  build: {
    outDir: '../../src/engineering_control/dashboard/console',
    emptyOutDir: true,
    assetsDir: 'assets',
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 4000,
    rollupOptions: {
      // Two pages. The sign-in page is served before there is a session, so the server
      // gives out exactly the files login.html names: its own entry, the stylesheet and
      // the libraries. The console's own code (the index entry) needs a session.
      input: { index: 'index.html', login: 'login.html' },
      output: {
        manualChunks: (id) => (id.includes('/node_modules/') ? 'vendor' : undefined),
      },
    },
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8750', '/login': 'http://127.0.0.1:8750' },
  },
});
