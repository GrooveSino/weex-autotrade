const userAgent = process.env.npm_config_user_agent ?? ''
const nodeMajor = Number(process.versions.node.split('.')[0])

if (!userAgent.startsWith('pnpm/11.15.1 ')) {
  console.error('此项目固定使用 pnpm 11.15.1，请先运行 corepack enable，再使用 pnpm install。')
  process.exit(1)
}

if (nodeMajor < 22 || nodeMajor >= 25) {
  console.error(`此项目要求 Node.js 22 或 24，当前版本为 ${process.version}。`)
  process.exit(1)
}
