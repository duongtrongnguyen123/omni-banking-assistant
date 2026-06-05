import React from "react";

/**
 * Last-line-of-defence error boundary. Any render-time exception inside
 * <App /> lands here so judges never see a blank white screen during a
 * live demo. Errors are logged to console for the dev team but the user
 * sees a Vietnamese-language friendly notice with a reload button.
 *
 * Class component because React Error Boundaries are only available via
 * `componentDidCatch` / `getDerivedStateFromError` — the hook equivalent
 * does not exist as of React 18.
 */
interface ErrorBoundaryState {
  error: Error | null;
}

interface ErrorBoundaryProps {
  children: React.ReactNode;
}

export class ErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error("[Omni] uncaught render error:", error, info);
  }

  private handleReload = (): void => {
    try {
      window.location.reload();
    } catch {
      /* no-op — only fails in test envs without window.location */
    }
  };

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div
          role="alert"
          aria-live="assertive"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            minHeight: "100vh",
            padding: "24px",
            fontFamily:
              "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
            backgroundColor: "#f7f8fa",
            color: "#111827",
            textAlign: "center",
          }}
        >
          <div
            style={{
              maxWidth: 420,
              padding: 24,
              borderRadius: 16,
              backgroundColor: "#ffffff",
              boxShadow: "0 12px 32px rgba(15, 23, 42, 0.12)",
            }}
          >
            <h1 style={{ fontSize: 20, margin: "0 0 8px 0" }}>
              Có lỗi xảy ra
            </h1>
            <p style={{ fontSize: 15, margin: "0 0 20px 0", color: "#4b5563" }}>
              Bạn thử tải lại trang nhé — Omni sẽ kết nối lại ngay.
            </p>
            <button
              type="button"
              onClick={this.handleReload}
              style={{
                appearance: "none",
                border: "none",
                cursor: "pointer",
                fontSize: 15,
                padding: "10px 22px",
                borderRadius: 999,
                backgroundColor: "#2563eb",
                color: "#ffffff",
                fontWeight: 600,
              }}
            >
              Tải lại trang
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
