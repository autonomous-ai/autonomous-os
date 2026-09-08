import React from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { AppearanceProvider } from './useAppearance'
import './styles.css'
import type { BuddyAPI } from '../shared/types'

declare global {
  interface Window {
    buddy: BuddyAPI
  }
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppearanceProvider><App /></AppearanceProvider>
  </React.StrictMode>,
)
