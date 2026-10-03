import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./tests',
  timeout:30_000,
  fullyParallel:false,
  use:{baseURL:'http://127.0.0.1:5173',viewport:{width:1024,height:900},headless:true},
  webServer:{command:'npm.cmd run dev -- --port 5173 --strictPort',url:'http://127.0.0.1:5173',reuseExistingServer:!process.env.CI,timeout:30_000},
});
