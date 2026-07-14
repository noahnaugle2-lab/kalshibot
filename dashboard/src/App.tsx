import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Header } from './components/Header';
import { KillModal } from './components/KillModal';
import { Toast } from './components/Toast';
import { useApp } from './store/store';
import { ConfigView } from './views/config/ConfigView';
import { HistoryView } from './views/history/HistoryView';
import { LeaderboardView } from './views/leaderboard/LeaderboardView';
import { LiveView } from './views/live/LiveView';
import { LoginView } from './views/login/LoginView';
import { SmartMoneyView } from './views/smartmoney/SmartMoneyView';
import { LiveDryRunView } from './views/live/LiveDryRunView';

// Routed views live inside an ErrorBoundary so a render crash (e.g. an
// unexpected null in a live payload) shows an inline panel instead of
// unmounting the whole app — Header, nav, and the KILL button stay mounted.
// Keying the boundary by pathname clears a stuck crash when you navigate away.
function RoutedViews() {
  const location = useLocation();
  return (
    <ErrorBoundary key={location.pathname}>
      <Routes location={location}>
        <Route path="/" element={<LiveView />} />
        <Route path="/leaderboard" element={<LeaderboardView />} />
        <Route path="/smartmoney" element={<SmartMoneyView />} />
        <Route path="/live-dry-run" element={<LiveDryRunView />} />
        <Route path="/history" element={<HistoryView />} />
        <Route path="/config" element={<ConfigView />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  );
}

export default function App() {
  const { authed } = useApp();

  if (!authed) {
    return (
      <>
        <LoginView />
        <Toast />
      </>
    );
  }

  return (
    <BrowserRouter>
      <div className="min-h-screen bg-bg font-mono text-fg pb-16">
        <Header />
        <RoutedViews />
        <KillModal />
        <Toast />
      </div>
    </BrowserRouter>
  );
}
