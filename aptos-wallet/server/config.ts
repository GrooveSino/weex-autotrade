import { resolve } from 'node:path'

export interface AppConfig {
  host: string
  port: number
  webOrigin: string
  databasePath: string
  webDistPath?: string
  executionEnabled: boolean
  fullnodeUrl?: string
  indexerUrl?: string
}

export const LOCAL_WALLET_HOST = '127.0.0.1'

export function assertLocalOnlyConfig(config: AppConfig): void {
  if (config.host !== LOCAL_WALLET_HOST) {
    throw new Error('安全限制：钱包服务只能监听 127.0.0.1')
  }
  if (!Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) {
    throw new Error('钱包服务端口必须在 1024 到 65535 之间')
  }
  let origin: URL
  try {
    origin = new URL(config.webOrigin)
  } catch {
    throw new Error('APTOS_WALLET_WEB_ORIGIN 格式无效')
  }
  if (origin.protocol !== 'http:' || origin.hostname !== LOCAL_WALLET_HOST || origin.origin !== config.webOrigin) {
    throw new Error('安全限制：网页来源必须是 http://127.0.0.1:<端口>，禁止公网域名、代理路径和远程地址')
  }
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const config: AppConfig = {
    host: env.APTOS_WALLET_HOST ?? LOCAL_WALLET_HOST,
    port: Number(env.APTOS_WALLET_API_PORT ?? '48271'),
    webOrigin: env.APTOS_WALLET_WEB_ORIGIN ?? 'http://127.0.0.1:48272',
    databasePath: resolve(env.APTOS_WALLET_DB_PATH ?? 'data/aptos-wallet.sqlite'),
    executionEnabled: env.APTOS_MAINNET_EXECUTION_ENABLED === 'true',
    fullnodeUrl: env.APTOS_FULLNODE_URL || undefined,
    indexerUrl: env.APTOS_INDEXER_URL || undefined,
  }
  assertLocalOnlyConfig(config)
  return config
}
