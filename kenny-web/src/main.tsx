import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Token load order matches the prototype's <helmet> exactly.
import './styles/tokens/fonts.css'
import './styles/tokens/colors.css'
import './styles/tokens/typography.css'
import './styles/tokens/spacing.css'
import './styles/tokens/effects.css'
import './styles/tokens/motion.css'
import './styles/tokens/base.css'
import './styles/global.css'

import App from './App'

const container = document.getElementById('root')
if (!container) throw new Error('#root element not found')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
