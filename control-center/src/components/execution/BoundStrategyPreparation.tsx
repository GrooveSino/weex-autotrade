import { LoaderCircle } from 'lucide-react'

export type PreparationStage = 'checking' | 'preflight' | null

const preparationCopy: Record<Exclude<PreparationStage, null>, { title: string; detail: string }> = {
  checking: {
    title: '正在检查当前策略任务',
    detail: '读取该账号已有授权状态，不会提交订单。',
  },
  preflight: {
    title: '正在生成启动确认',
    detail: '复用共享 Final Beta，并行读取余额、持仓与挂单。',
  },
}

export function BoundStrategyPreparation({ stage }: { stage: Exclude<PreparationStage, null> }) {
  return <section className="bound-preparation" aria-live="polite" aria-busy="true">
    <div className="bound-preparation-header">
      <LoaderCircle className="spin" size={17} />
      <div><strong>{preparationCopy[stage].title}</strong><p>{preparationCopy[stage].detail}</p></div>
    </div>
    <div className="bound-preparation-steps">
      <div className={`bound-preparation-step ${stage === 'checking' ? 'active' : 'done'}`}><span>1</span><div><strong>检查当前任务状态</strong><small>确认没有未结束的授权任务</small></div></div>
      <div className={`bound-preparation-step ${stage === 'preflight' ? 'active' : ''}`}><span>2</span><div><strong>读取共享 Beta 与账户边界</strong><small>余额与 BTC/ETH 只读链路受控并行</small></div></div>
      <div className="bound-preparation-step"><span>3</span><div><strong>生成精确确认短语</strong><small>完成后一次性展示最终确认</small></div></div>
    </div>
    <div className="bound-preparation-progress" aria-hidden="true"><span /></div>
    <p className="bound-preparation-note">首次只读预检通常需要数秒；已有未过期确认会立即打开。</p>
  </section>
}
