import { FormEvent, useState } from 'react'
import { ChevronsUp, KeyRound, LoaderCircle, LockKeyhole } from 'lucide-react'

type LocalLoginProps = {
  loading: boolean
  error: string | null
  onSubmit: (username: string, password: string) => Promise<void>
}

export function LocalLogin({ loading, error, onSubmit }: LocalLoginProps) {
  const [username, setUsername] = useState('gg')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!username.trim() || !password || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(username.trim(), password)
    } finally {
      setSubmitting(false)
      setPassword('')
    }
  }

  return (
    <main className="local-login-shell">
      <form className="local-login" onSubmit={(event) => void submit(event)}>
        <div className="local-login-brand"><ChevronsUp size={22} /><span>WEEX Fleet</span></div>
        <div className="local-login-copy">
          <h1>本机控制台登录</h1>
          <p>实例、策略、日志和后台执行器按本机用户隔离。登录不会中断已运行的任务。</p>
        </div>
        <label>
          <span><KeyRound size={13} />用户</span>
          <input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} disabled={loading || submitting} />
        </label>
        <label>
          <span><LockKeyhole size={13} />32 位本机密码</span>
          <input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} disabled={loading || submitting} />
        </label>
        {error && <p className="local-login-error" role="alert">{error}</p>}
        <button className="button primary local-login-submit" type="submit" disabled={loading || submitting || !username.trim() || !password}>
          {(loading || submitting) && <LoaderCircle className="spin" size={14} />}登录控制台
        </button>
      </form>
    </main>
  )
}
