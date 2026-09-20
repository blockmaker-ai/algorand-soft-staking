import {defineConfig} from 'vitest/config';
export default defineConfig({test:{include:['contracts/**/*.algo.e2e.test.ts'],environment:'node',maxWorkers:1,fileParallelism:false,testTimeout:30000,hookTimeout:30000}});
