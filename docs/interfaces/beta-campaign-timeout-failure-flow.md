# Beta Maker 超时与失败判定流程

本文说明当前 `live beta-volume` 和 `live beta-campaign` 在时间耗尽、连续无成交、部分成交、撤单确认失败等情况下的实际行为。

> 本文描述的是当前实现，不是交易所保证。所有成交、撤单和最终空仓状态都必须经过 WEEX 权威接口确认。

## 先看结论

`240 秒`是**单条交易腿的一次执行调用的累计总时限**。它从该腿开始执行时计时，覆盖：

- Maker 挂单前的盘口读取和限频等待；
- 当前挂单的成交轮询；
- 重新报价前的撤单和撤单确认；
- 每次重新报价后的等待。

它不是每张订单各自拥有的 240 秒，也不会因为部分成交或重新报价而重置。

| 层级 | 时间/次数边界 | 到达边界后的行为 |
| --- | --- | --- |
| 单张 Maker 订单 | 通常最多驻留 15 秒；盘口恶化时可在满 3 秒后提前撤 | 撤单一次，验证撤单结果；确认后才允许重新报价 |
| 单条交易腿 | 累计 240 秒 | 取消该交易对的普通单和条件单并逐类确认为空；确认成功后才能进入残仓 Maker 平仓；未完成则返回 `deadline_exceeded` |
| 撤单/清理确认 | 最多 5 次只读查询，间隔约 0.25、0.5、1、2 秒 | 单笔撤单或全量清理仍无法确认则分别返回 `deadline_cancel_not_confirmed` / `deadline_cleanup_not_confirmed`，进入不确定状态 |
| 单个 Beta 周期 | BTC/ETH 开仓、必要的 Maker 恢复平仓、最终空仓检查 | 空仓且无硬失败/不确定时可标记 `recovered` 并继续 |
| Beta 子会话 | 默认允许连续 3 个空周期 | 第 4 个空周期触发 `empty_round_limit_exhausted` |
| Beta Campaign | 最多 20 个有界 child session | 只对明确允许的软停止创建下一个 child；不确定或硬失败直接结束 |

## 失败分类

### 1. 可恢复的超时

单腿到达 240 秒后，如果满足以下条件，超时可以被周期级恢复：

1. 最后一张挂单已确认撤销，或已确认在撤单过程中成交；
2. 实际残余仓位可以读取；
3. 残余仓位通过纯 Maker 平仓；
4. BTC 和 ETH 最终都确认空仓；
5. 成交明细对账成功，且全部为 Maker。

此时周期状态是 `recovered`。它不是成功完成了原计划量，而是安全收敛到空仓，之后可以开始下一个周期。

### 2. 当前周期停止，但 Campaign 可能换 child

如果周期已经回到空仓，但该周期没有产生有效成交，或者达到了允许的空周期/轮次上限，子会话可能返回：

- `empty_round_limit_exhausted`
- `round_limit_exhausted`

这两个原因属于 Campaign 允许重试的软停止。Campaign 会重新读取空仓边界，创建下一个有界 child；最多受 `max_runs=20` 限制，不会无限循环。

### 3. 直接硬失败

下列情况不会通过“再等一会儿”解决，也不会降级为 GTC、市价单或追价：

- `post_only_rejected`
- `taker_fill_detected`
- `unknown_liquidity`
- `venue_did_not_accept_post_only`
- `policy_would_take_liquidity`
- `target_overfilled`

一旦出现这些结果，当前周期停止；Campaign 不会因为还有剩余目标量而继续提交新单。

### 4. 不确定失败

下列情况意味着程序无法证明真实状态，必须停止提交：

- `order_observation_unavailable`
- `cancel_not_confirmed`
- `deadline_cancel_not_confirmed`
- `deadline_cleanup_not_confirmed`
- `deadline_order_not_confirmed`
- `submission_uncertain`
- `active_order_remains_after_leg`
- `position_observation_unavailable`
- 成交明细无法对账或 Maker 属性无法确认

不确定不是“没成交”，而是“不能安全判断有没有成交”。这时程序会返回 `uncertain`，需要重新读取真实账户状态后，使用显式恢复流程处理，绝不自动重提同一意图。

## 单条交易腿：240 秒时序

```plantuml
@startuml
title 单条 BTC/ETH Maker 交易腿的累计超时流程

skinparam backgroundColor #FAFAFA
skinparam activity {
  BackgroundColor #FFFFFF
  BorderColor #555555
  ArrowColor #333333
}

start
:记录 leg_start_time;
:累计预算 = 240 秒;

note right
超时边界先取消本交易对的普通单和条件单，
逐类查询确认均为空；确认失败返回 uncertain，
不得提交任何平仓单。
确认成功后读取实际仓位，
由上层协调器使用 POST_ONLY Maker 平残仓。
end note

repeat
  :读取实际仓位与最新盘口;

  if (已有活动 Maker 单?) then (是)
    :只读查询订单状态;

    if (状态可确定?) then (否)
      if (连续读取错误达到上限?) then (是)
        :返回 uncertain\norder_observation_unavailable;
        stop
      else (否)
        :按退避间隔等待后重查;
      endif
    else (是)
      if (目标仓位已达到?) then (是)
        :记录已确认成交;
        :返回 completed / target_reached;
        stop
      endif

      :计算订单年龄、盘口陈旧度、成交概率;
      if (达到撤单条件?) then (是)
        note right
          撤单条件包括：
          - 盘口落后达到 stale_ticks
          - 成交概率过低且预计等待过长
          - 订单年龄达到 max_rest_ms=15s
          - 极端不利选择
        end note
        :只提交一次撤单请求;
        :最多 5 次只读确认撤单结果;

        if (撤单状态已确认?) then (否)
          :返回 uncertain\ncancel_not_confirmed;
          stop
        else (是)
          if (撤单过程中已补足目标?) then (是)
            :返回 completed;
            stop
          else (否)
            :清除当前活动单;
            :剩余仓位允许重新报价;
          endif
        endif
      else (否)
        :每约 250ms 查询一次;
      endif
    endif
  else (否)
    if (累计时间已达到 240 秒?) then (是)
      :进入最终撤单分支;
    else (否)
      :按最新盘口计算被动价格;
      if (价格会吃单?) then (是)
        :返回 failed\npolicy_would_take_liquidity;
        stop
      else (否)
        :提交一张 POST_ONLY Maker 单;
      endif
    endif
  endif

  if (累计时间已达到 240 秒?) then (是)
    if (仍有活动订单?) then (是)
      :只提交一次撤单并验证;
      if (撤单验证失败?) then (是)
        :返回 uncertain\ndeadline_cancel_not_confirmed;
        stop
      endif
    endif
    if (目标仓位已达到?) then (是)
      :返回 completed;
      stop
    else (否)
      :返回 failed\ndeadline_exceeded;
      stop
    endif
  endif
repeat while (目标仓位未达到且仍在预算内?) is (继续)

stop
@enduml
```

## 一个周期：开仓失败后如何收敛

BTC 多仓和 ETH 空仓使用两个独立通道并发执行。开仓阶段结束后，不论一条腿成功、一条腿超时，还是两条腿都部分成交，主线程都会读取实际仓位，再决定需要哪些平仓任务。

```plantuml
@startuml
title BTC 多 + ETH 空配对周期的失败与恢复

skinparam backgroundColor #FAFAFA
skinparam activity {
  BackgroundColor #FFFFFF
  BorderColor #555555
  ArrowColor #333333
}

start
:周期边界已确认空仓;
:并发启动 BTC long 开仓\n和 ETH short 开仓;

fork
  :BTC Maker leg;
  if (completed?) then (是)
    :BTC 开仓成交已对账;
  elseif (deadline_exceeded?) then (超时)
    :BTC leg stopped;
  else (硬失败/不确定)
    :BTC leg stop;
  endif
fork again
  :ETH Maker leg;
  if (completed?) then (是)
    :ETH 开仓成交已对账;
  elseif (deadline_exceeded?) then (超时)
    :ETH leg stopped;
  else (硬失败/不确定)
    :ETH leg stop;
  endif
end fork

if (存在 submission_uncertain?) then (是)
  :禁止对不确定通道继续提交;
  :对另一条状态确定的通道做安全 Maker 平仓;
  :周期 = uncertain;
  :停止当前子会话;
  stop
else (否)
  if (两腿都确认开仓?) then (是)
    :按 hold_min ~ hold_max 等待;
  else (否)
    :跳过持仓等待;
  endif
endif

:读取 BTC/ETH 实际仓位;
:并发启动实际残余仓位的 Maker 平仓;

fork
  :BTC flatten lane;
  if (最多 3 次恢复后空仓?) then (是)
    :BTC flat;
  else (否)
    :BTC stopped/uncertain;
  endif
fork again
  :ETH flatten lane;
  if (最多 3 次恢复后空仓?) then (是)
    :ETH flat;
  else (否)
    :ETH stopped/uncertain;
  endif
end fork

:重新读取两腿仓位、普通单、条件单和 userTrades;

if (两腿空仓且无不确定状态?) then (是)
  if (出现 Taker/未知流动性/POST_ONLY 硬失败?) then (是)
    :周期 = stopped;
    :停止当前子会话;
    stop
  else (否)
    if (有有效成交?) then (是)
      :周期 = recovered;
    else (否)
      :周期 = empty;
    endif
    :允许进入下一周期;
  endif
else (否)
  :周期 = stopped 或 uncertain;
  :禁止继续开仓;
  stop
endif

stop
@enduml
```

## Campaign 层：什么时候继续，什么时候结束

`live beta-campaign` 不是把一个 240 秒预算无限延长，而是最多创建 20 个有界 child。每个 child 都从已确认空仓边界开始，并在结束时再次检查空仓和挂单。

```plantuml
@startuml
title Beta Campaign 的 child 重试与最终结束

skinparam backgroundColor #FAFAFA
skinparam activity {
  BackgroundColor #FFFFFF
  BorderColor #555555
  ArrowColor #333333
}

start
:读取 campaign 计划;
:确认 profile、Beta、账户边界;

if (账户边界可读且确认空仓?) then (否)
  :返回 uncertain/stopped;
  stop
else (是)
  :创建 child plan;
endif

while (Campaign 目标未达到且 child 数 < 20?) is (继续)
  :执行一个 child;
  :child 内部按周期执行\n每条 leg 240 秒累计预算;

  if (child status = completed?) then (是)
    if (Campaign 目标已达到?) then (是)
      :最终验收：空仓、无普通单、无条件单、Maker 对账;
      if (验收通过?) then (是)
        :Campaign completed;
        stop
      else (否)
        :Campaign uncertain;
        stop
      endif
    else (否)
      :读取 child 检查点;
      :准备下一个 child;
    endif
  elseif (child reason = empty_round_limit_exhausted\n或 round_limit_exhausted?) then (软停止)
    :重新读取最终边界;
    if (空仓且无挂单?) then (是)
      if (已使用 20 个 child?) then (是)
        :Campaign stopped\nround_limit_exhausted;
        stop
      else (否)
        :允许创建下一个 child;
      endif
    else (否)
      :Campaign uncertain;
      stop
    endif
  elseif (child status = uncertain?) then (是)
    :禁止自动创建下一个 child;
    :返回 uncertain;
    stop
  else (硬失败或其他停止)
    :禁止自动创建下一个 child;
    :返回 stopped;
    stop
  endif
endwhile (目标已达到或 child 已用尽)

stop
@enduml
```

## “连续 6 次失败”到底对应什么

这个说法可能对应三种不同计数，程序行为不同：

### 连续 6 次重新报价

不会因为第 6 次就直接失败。重新报价最多受两道边界限制：

- 单腿累计 240 秒；
- `max_requotes=30`。

实际通常是 240 秒先到，因为每次重新报价还要消耗撤单确认和重新挂单时间。

### 连续 6 个空周期

默认 `max_empty_rounds=3`，所以同一个 child 不会等到第 6 个空周期：

```text
第 1 个空周期：继续
第 2 个空周期：继续
第 3 个空周期：继续
第 4 个空周期：停止当前 child
```

如果这是 Campaign 而不是单独的 `beta-volume`，并且第 4 个空周期结束时已经确认空仓，Campaign 可以把这个软停止转换为下一个 child；但总 child 数仍不超过 20。

### 连续 6 次提交都没有成交

程序不会单纯按“提交次数”判断安全。它会在每次提交后观察：

- 当前订单状态是否可读；
- 是否有部分成交；
- Maker/Taker 属性是否明确；
- 撤单是否确认；
- 实际仓位是否与成交明细一致。

因此即使只提交了 2 次，只要第二次撤单状态不确定，也会立即进入 `uncertain`；反过来，即使提交了 6 次，只要在 240 秒内都被明确撤销、最终实际仓位被 Maker 平掉，也可能只是一次 `recovered` 周期。

## 代码对应关系

- 单腿累计 deadline、最终撤单和 `deadline_exceeded`：[`adaptive_executor.py`](../../src/weex_cli/adaptive_executor.py)
- Maker 订单的最短/最长驻留与盘口决策：[`adaptive_maker.py`](../../src/weex_cli/adaptive_maker.py)
- BTC/ETH 并发开仓、实际仓位平仓与周期判定：[`beta_volume.py`](../../src/weex_cli/beta_volume.py)
- Campaign child 的软停止重试与不确定状态终止：[`beta_campaign.py`](../../src/weex_cli/beta_campaign.py)
