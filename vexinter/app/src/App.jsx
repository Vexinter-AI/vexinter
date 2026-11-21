import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { ThemeProvider } from './theme';
import Ambient from './components/Ambient';
import Layout from './components/Layout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Executions from './pages/Executions';
import TaskDetail from './pages/TaskDetail';
import Cases from './pages/Cases';
import Agents from './pages/Agents';
import Metrics from './pages/Metrics';
import { checkSession, isAuthed } from './api';

const Guard = ({ children }) => isAuthed() ? children
  : <Navigate to="/login" replace />;

// Validates the session cookie once on load (the sessionStorage flag
// does not survive a new tab, the HttpOnly cookie does).
function AuthGate({ children }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    checkSession().finally(() => setReady(true));
  }, []);
  return ready ? children : null;
}

export default function App() {
  return <ThemeProvider><Ambient /><BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '')}>
    <AuthGate><Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Layout />}>
        <Route path="/" element={<Guard><Dashboard /></Guard>} />
        <Route path="/executions" element={<Guard><Executions /></Guard>} />
        <Route path="/tasks/:id" element={<Guard><TaskDetail /></Guard>} />
        <Route path="/cases" element={<Guard><Cases /></Guard>} />
        <Route path="/agents" element={<Guard><Agents /></Guard>} />
        <Route path="/metrics" element={<Guard><Metrics /></Guard>} />
      </Route>
    </Routes></AuthGate>
  </BrowserRouter></ThemeProvider>;
}