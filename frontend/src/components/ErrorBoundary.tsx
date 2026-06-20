import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div style={{
          padding: '40px 20px',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '300px',
          textAlign: 'center',
        }}>
          <span style={{ fontSize: 48, marginBottom: 16 }}>⚠️</span>
          <h3 style={{ color: 'var(--text-secondary)', marginBottom: 8 }}>页面加载异常</h3>
          <p style={{ color: 'var(--text-muted)', fontSize: 14, maxWidth: 480, marginBottom: 20 }}>
            {this.state.error?.message || '未知错误'}
          </p>
          <button
            onClick={() => {
              this.setState({ hasError: false, error: null });
              window.location.reload();
            }}
            className="btn btn-primary"
          >
            刷新页面
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
