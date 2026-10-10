import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import WebApp from './WebApp.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <WebApp />
  </StrictMode>,
)
