import { resolve } from 'node:path'

export interface AppConfig {
  host: string
  port: number
  webOrigin: string
  databasePath: string
  executionEnabled: boolean
  fullnodeUrl?: string
  indexerUrl?: string
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const host = env.APTOS_WALLET_HOST ?? '127.0.0.1'
  if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') {
    throw new Error('APTOS Wallet v1 只允许监听本机回环地址')
  }
  return {
    host,
    port: Number(env.APTOS_WALLET_API_PORT ?? '4311'),
    webOrigin: env.APTOS_WALLET_WEB_ORIGIN ?? 'http://127.0.0.1:4310',
    databasePath: resolve(env.APTOS_WALLET_DB_PATH ?? 'data/aptos-wallet.sqlite'),
    executionEnabled: env.APTOS_MAINNET_EXECUTION_ENABLED === 'true',
    fullnodeUrl: env.APTOS_FULLNODE_URL || undefined,
    indexerUrl: env.APTOS_INDEXER_URL || undefined,
  }
}
