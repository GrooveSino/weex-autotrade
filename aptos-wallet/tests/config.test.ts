import { describe, expect, it } from 'vitest'
import { assertLocalOnlyConfig, loadConfig, type AppConfig } from '../server/config.js'

const localConfig: AppConfig = {
  host: '127.0.0.1',
  port: 4311,
  webOrigin: 'http://127.0.0.1:4310',
  databasePath: 'data/aptos-wallet.sqlite',
  executionEnabled: false,
}

describe('local-only runtime configuration', () => {
  it('accepts only the fixed IPv4 loopback listener and a plain loopback web origin', () => {
    expect(() => assertLocalOnlyConfig(localConfig)).not.toThrow()
    expect(() => assertLocalOnlyConfig({ ...localConfig, host: '0.0.0.0' })).toThrow('只能监听 127.0.0.1')
    expect(() => assertLocalOnlyConfig({ ...localConfig, host: 'localhost' })).toThrow('只能监听 127.0.0.1')
    expect(() => assertLocalOnlyConfig({ ...localConfig, webOrigin: 'https://wallet.example.com' })).toThrow('禁止公网域名')
    expect(() => assertLocalOnlyConfig({ ...localConfig, webOrigin: 'http://127.0.0.1:4310/proxy' })).toThrow('代理路径')
  })

  it('rejects unsafe environment overrides before opening the database', () => {
    expect(() => loadConfig({ APTOS_WALLET_HOST: '0.0.0.0' } as NodeJS.ProcessEnv)).toThrow('只能监听 127.0.0.1')
    expect(() => loadConfig({ APTOS_WALLET_WEB_ORIGIN: 'https://public.example' } as NodeJS.ProcessEnv)).toThrow('网页来源必须是')
    expect(() => loadConfig({ APTOS_WALLET_API_PORT: '80' } as NodeJS.ProcessEnv)).toThrow('端口必须在')
  })
})
