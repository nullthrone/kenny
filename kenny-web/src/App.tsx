import { QueryClientProvider } from '@tanstack/react-query'
import { HashRouter } from 'react-router'
import { queryClient } from './api/queryClient'
import { ThemeProvider } from './theme/ThemeProvider'
import AppRoutes from './router/routes'

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <HashRouter>
          <AppRoutes />
        </HashRouter>
      </ThemeProvider>
    </QueryClientProvider>
  )
}
