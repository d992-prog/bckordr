import { Component, type ErrorInfo, type ReactNode } from "react";

interface PortalErrorBoundaryProps {
  children: ReactNode;
}

interface PortalErrorBoundaryState {
  failed: boolean;
}

export class PortalErrorBoundary extends Component<
  PortalErrorBoundaryProps,
  PortalErrorBoundaryState
> {
  state: PortalErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): PortalErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Veltrix portal render failed", error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <main className="state-page">
          <section className="card state-card vx-matte" role="alert">
            <h1>Не удалось отобразить кабинет</h1>
            <p>Закройте это окно и откройте Mini App снова.</p>
            <button
              type="button"
              className="button button--primary"
              onClick={() => window.location.reload()}
            >
              Попробовать ещё раз
            </button>
          </section>
        </main>
      );
    }
    return this.props.children;
  }
}
