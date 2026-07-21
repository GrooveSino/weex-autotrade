import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles.css'

if (window.location.hostname !== '127.0.0.1') {
  throw new Error('安全限制：Aptos 本地钱包只能通过 127.0.0.1 打开')
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
