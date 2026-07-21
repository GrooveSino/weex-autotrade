import { createApp } from './app.js'
import { loadConfig } from './config.js'

process.umask(0o077)

const config = loadConfig()
const app = await createApp(config)

await app.listen({ host: config.host, port: config.port })
console.log(`Aptos 本地钱包 API: http://${config.host}:${config.port}`)
console.log(`Mainnet execution: ${config.executionEnabled ? 'ENABLED' : 'DISABLED (preview only)'}`)
