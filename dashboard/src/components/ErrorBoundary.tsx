import { Component, type ReactNode } from 'react';

/**
 * Last line of defense: a view that throws renders an inline error panel
 * instead of unmounting the whole app to a black page. Header, nav, and the
 * kill switch stay reachable no matter what a data payload does to a view.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error('view crashed:', error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="max-w-[860px] mx-auto mt-10 px-5">
          <div className="border border-redborder bg-redbg rounded-md p-4 text-[10px]">
            <div className="text-red font-extrabold tracking-[0.1em] text-[11px] mb-2">
              VIEW CRASHED
            </div>
            <div className="text-dim mb-2">
              This view hit an error rendering live data. The bot, feeds, and kill
              switch are unaffected — this is a display failure only.
            </div>
            <pre className="text-faint whitespace-pre-wrap break-all mb-3">
              {String(this.state.error)}
            </pre>
            <button
              onClick={() => this.setState({ error: null })}
              className="border border-strong rounded px-2.5 py-1 text-[9px] font-bold tracking-[0.08em] text-dim hover:border-indigo hover:text-fg"
            >
              RETRY VIEW
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
