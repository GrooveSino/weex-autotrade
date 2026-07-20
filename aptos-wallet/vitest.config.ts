import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    coverage: { include: ['server/**/*.ts', 'shared/**/*.ts'] },
  },
})
