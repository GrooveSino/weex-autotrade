export type StrategyPanelOperation = 'prepare' | 'cleanup' | 'confirm' | 'stop' | 'verify'

export type StrategyPanelAction =
  | 'retry_prepare'
  | 'verify_execution'
  | 'edit_account'
  | 'open_beta_settings'
  | 'reload_page'
  | 'none'

export interface StrategyPanelIssue {
  title: string
  reason: string
  nextStep: string
  action: StrategyPanelAction
  actionLabel?: string
}

interface ErrorContext {
  reasonCode?: string | null
  disposition?: string | null
}

const issue = (
  title: string,
  reason: string,
  nextStep: string,
  action: StrategyPanelAction,
  actionLabel?: string,
): StrategyPanelIssue => ({ title, reason, nextStep, action, actionLabel })

function rawMessage(value: unknown): string {
  if (value instanceof Error) return value.message
  return typeof value === 'string' ? value : ''
}

function includesAny(value: string, patterns: string[]): boolean {
  return patterns.some((pattern) => value.includes(pattern))
}

export function strategyPanelError(
  value: unknown,
  operation: StrategyPanelOperation,
  context: ErrorContext = {},
): StrategyPanelIssue {
  const source = `${context.reasonCode ?? ''} ${context.disposition ?? ''} ${rawMessage(value)}`.toLowerCase()

  if (includesAny(source, ['local login is required', 'unauthorized', 'http 401'])) {
    return issue('登录状态已失效', '当前登录会话已过期，控制面拒绝了本次请求。', '刷新页面并重新登录，然后重新打开该账号的启动面板。', 'reload_page', '刷新并重新登录')
  }
  if (includesAny(source, ['credentials_unavailable', 'credentials are unavailable', '账号凭据不可用'])) {
    return issue('账号凭据不可用', '该账号缺少可用的 API 凭据，无法完成启动边界核验。', '打开账号编辑，补全并保存凭据，然后重新启动策略。', 'edit_account', '编辑账号')
  }
  if (includesAny(source, ['requires a live account', 'live account'])) {
    return issue('账号模式不符合要求', '已绑定策略只能由实盘账号启动。', '打开账号编辑，将账号模式改为实盘并保存，再重新启动。', 'edit_account', '编辑账号')
  }
  if (includesAny(source, ['final_beta_unavailable', 'final beta source unavailable', 'final beta 来源', 'beta snapshot unavailable'])) {
    return issue('Final Beta 暂不可用', '系统无法取得可用于生成本次计划的 Final Beta。', '打开 Beta 来源设置，修正来源并保存；数据恢复后点击重新获取确认。', 'open_beta_settings', '打开 Beta 来源')
  }
  if (includesAny(source, ['boundary_unavailable', '账户持仓与挂单边界暂时不可用', 'account boundary has an invalid'])) {
    return issue('账户边界读取失败', '余额、仓位或挂单数据暂时无法完整读取，本次没有提交订单。', '确认代理和交易所连接可用后，点击重新检查。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['complete lifetime trade history synchronization', 'history baseline', '成交历史基线'])) {
    return issue('成交历史基线待核验', '累计策略所需的历史成交基线尚未准备完成。', '等待后台只读同步完成后，点击重新获取确认。', 'retry_prepare', '重新获取确认')
  }
  if (includesAny(source, ['no remaining verified target', 'volume target has already been reached'])) {
    return issue('策略目标已经完成', '权威成交账本显示该策略没有剩余目标量。', '关闭此窗口；如需继续运行，请先在策略库提高目标范围，再重新启动。', 'none')
  }
  if (includesAny(source, ['account_is_not_flat', 'is no longer flat'])) {
    return issue('账户当前不是空仓状态', '启动前检测到已有仓位或挂单，系统不会把它们当作本次任务仓位。', '在交易所关闭已有仓位并撤销挂单，然后点击重新检查。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['available_balance_insufficient', 'funding preflight failed', 'invalid available balance'])) {
    return issue('启动预检未通过', '当前余额或计划参数不满足启动要求，本次没有提交订单。', '补足可用余额或调整已绑定策略参数，然后点击重新获取确认。', 'retry_prepare', '重新获取确认')
  }
  if (includesAny(source, ['preview blocked'])) {
    return issue('启动边界未通过', '账户边界不满足当前策略启动条件，本次没有提交订单。', '点击重新检查获取最新边界；按面板显示的仓位、挂单或余额提示处理后继续。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['strategy changed since preview', 'authorization has expired', 'schema is not executable', '启动条件已变化'])) {
    return issue('启动确认已经失效', '策略版本、账户边界或确认有效期已经变化，旧确认不能继续使用。', '点击重新获取确认，并使用新生成的确认短语启动。', 'retry_prepare', '重新获取确认')
  }
  if (includesAny(source, ['exact campaign confirmation does not match', '确认短语不匹配'])) {
    const cleanup = operation === 'cleanup'
    return issue(cleanup ? '撤单确认短语不匹配' : '启动确认短语不匹配', '输入内容与当前页面生成的精确确认短语不一致。', `点击复制按钮，完整粘贴${cleanup ? '撤单' : '启动'}确认短语后再次提交。`, 'none')
  }
  if (includesAny(source, ['exact stop confirmation does not match'])) {
    return issue('停止确认短语不匹配', '输入内容与当前任务的停止确认短语不一致。', '点击复制按钮，完整粘贴停止确认短语后再次点击安全停止。', 'none')
  }
  if (includesAny(source, ['risk acknowledgement is required'])) {
    return issue('尚未确认实盘风险', '启动请求缺少当前预览所需的风险确认。', '勾选实盘风险确认，并重新粘贴精确确认短语后启动。', 'none')
  }
  if (includesAny(source, ['capacity is full', 'capacity_full', '恢复容量'])) {
    return issue('执行容量暂时已满', '执行器当前没有空闲生命周期容量，但当前预览没有被提交。', '等待其他任务退出后，保持本页打开并再次点击确认；若预览过期，先重新获取确认。', operation === 'confirm' ? 'none' : 'retry_prepare', operation === 'confirm' ? undefined : '重新检查容量')
  }
  if (includesAny(source, ['already has an active execution', 'already has an active beta campaign', 'already in use by another live campaign', 'already starting or running', 'already claimed', '旧任务仍在执行'])) {
    return issue('该账号已有活动任务', '同一账号已有任务正在启动、运行或停止，系统不会创建第二个任务。', '点击刷新核验状态；待现有任务进入终态后再生成新的启动确认。', 'verify_execution', '刷新核验状态')
  }
  if (includesAny(source, ['command already accepted', 'command id conflicts', 'already accepted'])) {
    return issue('命令已经被控制面接收', '浏览器没有拿到最终响应，但同一命令不能再次提交。', '点击刷新核验状态，读取该任务的实际状态；不要重复点击启动、撤单或停止。', 'verify_execution', '刷新核验状态')
  }
  if (includesAny(source, ['x-fleet-command-id is required', 'invalid command id'])) {
    return issue('请求标识生成失败', '浏览器未能生成有效的幂等命令标识，本次命令未被正常受理。', '刷新页面以重新初始化控制台，然后重新打开启动面板。', 'reload_page', '刷新页面')
  }
  if (includesAny(source, ['field required', 'input should be', 'unprocessable content', 'http 422'])) {
    return issue('页面请求格式已失效', '当前网页版本与控制面接口不一致，服务端拒绝了请求且没有执行命令。', '刷新页面加载最新版本，然后重新打开该账号的启动面板。', 'reload_page', '刷新页面')
  }
  if (includesAny(source, ['撤单正在执行'])) {
    return issue('撤单正在核验', '该账号已有一条启动前撤单命令正在处理。', '不要重复撤单；点击重新检查，直到挂单数量更新。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['撤单结果尚未核验', '撤单后无法读取', 'cancellation could not be verified'])) {
    return issue('撤单结果暂时无法确认', '撤单请求可能已经生效，但挂单状态尚未完成只读核验。', '不要重复发送撤单；点击重新检查，确认普通单和条件单均为零后继续。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['当前没有需要撤销', 'no open positions'])) {
    return issue('账户边界已经变化', '页面中的待处理状态已经过期，当前没有对应的可执行对象。', '点击重新检查，使用最新账户边界继续启动。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['无法证明属于本次任务', 'live profile changed', 'account changed after preview'])) {
    return issue('仓位归属无法确认', '当前账户状态与任务固化边界不一致，系统不会自动处理来源不明的仓位。', '在交易所核对并关闭已有仓位与挂单，然后点击重新检查。', 'retry_prepare', '重新检查')
  }
  if (includesAny(source, ['campaign is not running', 'worker is not available', 'not in planned state'])) {
    return issue('任务状态已经变化', '当前操作对应的任务阶段已结束或正在由后台收尾。', '点击刷新核验状态，按最新状态继续；不要重复提交原命令。', 'verify_execution', '刷新核验状态')
  }
  if (includesAny(source, ['execution was not created from this account', 'account mismatch', 'owner mismatch', 'not found'])) {
    return issue('任务与当前账号不匹配', '当前窗口引用的任务已失效或不属于这个账号。', '关闭窗口，从账号列表重新点击启动以生成正确的任务上下文。', 'none')
  }
  if (includesAny(source, ['execution could not establish its local ledger session', 'worker could not be started', 'manager is shutting down', 'orchestrator is closed', '执行器未启用', 'disabled'])) {
    return issue('执行器暂时不可用', '执行服务正在切换或没有可用运行资源，本次没有继续提交订单。', '等待控制台恢复“实盘执行可用”后，点击重新获取确认。', 'retry_prepare', '重新获取确认')
  }
  if (includesAny(source, ['failed to fetch', 'networkerror', 'load failed', 'timeout', 'control-plane request failed', 'internal server error', 'http 500', 'http 502', 'http 503', 'http 504'])) {
    const ambiguous = operation === 'confirm' || operation === 'cleanup' || operation === 'stop'
    return issue('控制面连接异常', ambiguous ? '请求响应中断，命令是否已被接收需要只读核验。' : '只读预检未能取得完整响应，本次没有提交订单。', ambiguous ? '点击刷新核验状态，确认实际任务和账户状态；不要重复提交原命令。' : '确认网络恢复后点击重新获取确认。', ambiguous ? 'verify_execution' : 'retry_prepare', ambiguous ? '刷新核验状态' : '重新获取确认')
  }

  if (operation === 'confirm' || operation === 'cleanup' || operation === 'stop' || operation === 'verify') {
    return issue('操作结果需要核验', '控制面返回了未识别的异常，无法确认命令是否已经生效。', '点击刷新核验状态，读取任务与账户的实际状态；不要重复提交原命令。', 'verify_execution', '刷新核验状态')
  }
  return issue('启动条件读取失败', '控制面返回了未识别的异常，本次没有提交订单。', '确认网络、代理和执行器状态正常后，点击重新获取确认。', 'retry_prepare', '重新获取确认')
}

export function strategyPanelNotice(
  title: string,
  reason: string,
  nextStep: string,
  action: StrategyPanelAction,
  actionLabel?: string,
): StrategyPanelIssue {
  return issue(title, reason, nextStep, action, actionLabel)
}
