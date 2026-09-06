import { Navigate, Route, Routes } from 'react-router-dom'
import ProtectedRoute from './components/ProtectedRoute'
import Shell from './components/Shell'
import AccountDetail from './pages/AccountDetail'
import Calibrations from './pages/Calibrations'
import Interventions from './pages/Interventions'
import Login from './pages/Login'
import Portfolio from './pages/Portfolio'
import ReviewQueue from './pages/ReviewQueue'
import Roi from './pages/Roi'
import Settings from './pages/Settings'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Shell />}>
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/interventions" element={<Interventions />} />
          <Route path="/review-queue" element={<ReviewQueue />} />
          <Route path="/roi" element={<Roi />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/calibrations" element={<Calibrations />} />
          <Route path="/accounts/:accountId" element={<AccountDetail />} />
          <Route path="/" element={<Navigate to="/portfolio" replace />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
