import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  fallback: (error: Error, reset: () => void) => ReactNode
  children: ReactNode
}

interface State {
  error: Error | null
}

/** Catches a failed load (the backend down, a missing entity) and shows it in place. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(error, info.componentStack)
  }

  render() {
    if (this.state.error) return this.props.fallback(this.state.error, () => this.setState({ error: null }))
    return this.props.children
  }
}
