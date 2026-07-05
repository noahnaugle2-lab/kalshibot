import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
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
        <Routes>
          <Route path="/" element={<LiveView />} />
          <Route path="/leaderboard" element={<LeaderboardView />} />
          <Route path="/smartmoney" element={<SmartMoneyView />} />
          <Route path="/history" element={<HistoryView />} />
          <Route path="/config" element={<ConfigView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <KillModal />
        <Toast />
      </div>
    </BrowserRouter>
  );
}
