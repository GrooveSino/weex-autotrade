export const ASSETS = {
  APT: {
    id: 'APT',
    name: 'Aptos',
    symbol: 'APT',
    decimals: 8,
    metadataAddress: null,
  },
  USDT: {
    id: 'USDT',
    name: 'Tether USD',
    symbol: 'USDt',
    decimals: 6,
    metadataAddress: '0x357b0b74bc833e95a115ad22604854d6b0fca151cecd94111770e5d6ffc9dc2b',
  },
} as const

export type AssetId = keyof typeof ASSETS
export type AmountMode = 'fixed' | 'random' | 'max'
export type WalletSource = 'generated' | 'private_key' | 'mnemonic'
export type WalletAccountStatus = 'standalone' | 'unused' | 'used' | 'funded'
export type WalletDerivationProfile = 'aptos_hd' | 'legacy_custom'
export type JobStatus = 'draft' | 'previewed' | 'running' | 'paused' | 'cancelled' | 'failed' | 'uncertain' | 'completed'
export type StepStatus = 'pending' | 'waiting' | 'preparing' | 'submitting' | 'confirmed' | 'failed' | 'uncertain' | 'cancelled'

export interface AssetBalance {
  asset: AssetId
  baseUnits: string
  display: string
}

export interface WalletRecord {
  id: string
  label: string
  address: string
  source: WalletSource
  groupId: string | null
  accountIndex: number | null
  accountStatus: WalletAccountStatus
  derivationPath: string | null
  createdAt: string
  balances: AssetBalance[]
  balanceError: string | null
  balanceUpdatedAt: string | null
  archivedAt: string | null
}

export interface WalletGroup {
  id: string
  label: string
  source: 'mnemonic'
  derivationProfile: WalletDerivationProfile
  nextAccountIndex: number
  activeAccountCount: number
  totalAccountCount: number
  archivedAt: string | null
  accounts: WalletRecord[]
  balances: AssetBalance[]
  createdAt: string
  updatedAt: string
}

export interface AddressBookEntry {
  id: string
  label: string
  address: string
  createdAt: string
  updatedAt: string
}

export interface DerivedAccountPreview {
  accountIndex: number
  derivationPath: string
  address: string
}

export interface MnemonicRestorePreview {
  accounts: DerivedAccountPreview[]
}

export interface EncryptedSecretResponse {
  algorithm: 'RSA-OAEP-256'
  ciphertext: string
}

export interface TransferStepDraft {
  id: string
  sourceWalletId: string
  targetAddress: string
  targetWalletId: string | null
  asset: AssetId
  amountMode: AmountMode
  amountMin: string | null
  amountMax: string | null
}

export interface FrozenTransferStep extends TransferStepDraft {
  position: number
  frozenAmountBaseUnits: string | null
  frozenAmountDisplay: string | null
  waitAfterSeconds: number
  status: StepStatus
  txHash: string | null
  gasFeeBaseUnits: string | null
  error: string | null
  /** Persisted timestamp used by the UI to reconstruct a waiting countdown. */
  updatedAt?: string
}

export interface TransferJob {
  id: string
  name: string
  status: JobStatus
  steps: FrozenTransferStep[]
  gasPayerWalletId: string | null
  intervalMinSeconds: number
  intervalMaxSeconds: number
  shuffle: boolean
  confirmationPhrase: string | null
  summary: JobSummary | null
  createdAt: string
  updatedAt: string
  error: string | null
}

export interface JobSummary {
  sourceWalletCount: number
  stepCount: number
  aptBaseUnits: string
  usdtBaseUnits: string
  maxStepCount: number
  estimatedGasBaseUnits: string
  warnings: string[]
}

export interface TransferStepCheck {
  stepId: string
  position: number
  valid: boolean
  error: string | null
  estimatedGasBaseUnits: string
  gasWalletId: string
  gasBalanceBaseUnits: string
}

export interface JobPreflight {
  valid: boolean
  job: TransferJob
  checks: TransferStepCheck[]
  summary: JobSummary | null
}

export interface TransactionAttempt {
  id: string
  jobId: string
  stepId: string
  senderAddress: string
  sequenceNumber: string | null
  txHash: string | null
  state: 'prepared' | 'submitted' | 'confirmed' | 'failed' | 'uncertain'
  gasFeeBaseUnits: string | null
  error: string | null
  createdAt: string
  updatedAt: string
}

export interface AccountTransferLog {
  id: string
  jobId: string
  jobName: string
  jobStatus: JobStatus
  position: number
  direction: 'in' | 'out'
  counterpartyAddress: string
  counterpartyWalletId: string | null
  asset: AssetId
  amountMode: AmountMode
  amountMin: string | null
  amountMax: string | null
  frozenAmountDisplay: string | null
  status: StepStatus
  txHash: string | null
  gasFeeBaseUnits: string | null
  error: string | null
  createdAt: string
  updatedAt: string
}

export interface AccountTransferLogPage {
  items: AccountTransferLog[]
  total: number
  counts: { all: number; in: number; out: number }
}

export interface VaultStatus {
  initialized: boolean
  unlocked: boolean
  executionEnabled: boolean
  network: 'mainnet'
  csrfToken: string
}

export interface JobDraftInput {
  name: string
  steps: TransferStepDraft[]
  gasPayerWalletId: string | null
  intervalMinSeconds: number
  intervalMaxSeconds: number
  shuffle: boolean
}
