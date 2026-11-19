import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import ProtectedRoute from './components/ProtectedRoute'
import Shell from './components/Shell'
import { useAuth } from './auth/AuthContext'
import AccountDetail from './pages/AccountDetail'
import Activity from './pages/Activity'
import AskAI from './pages/AskAI'
import Calibrations from './pages/Calibrations'
import Interventions from './pages/Interventions'
import JourneyCanvas from './pages/JourneyCanvas'
import Login from './pages/Login'
import Platform, { PlatformTenant } from './pages/Platform'
import PlatformOnboarding, { PlatformOnboardingTenant } from './pages/PlatformOnboarding'
import Portfolio from './pages/Portfolio'
import ReviewQueue from './pages/ReviewQueue'
import Roi from './pages/Roi'
import Settings from './pages/Settings'

// three.js is large, so the 3D graph page is fetched only when someone opens it.
const AccountGraph = lazy(() => import('./pages/AccountGraph'))

// A console-only operator belongs to no tenant, so the tenant pages have nothing to show them.
function Home() {
  const { user } = useAuth()
  return <Navigate to={user?.platform_role && !user.customer_id ? '/platform' : '/portfolio'} replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Shell />}>
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/ask" element={<AskAI />} />
          <Route path="/interventions" element={<Interventions />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/review-queue" element={<ReviewQueue />} />
          <Route path="/roi" element={<Roi />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/calibrations" element={<Calibrations />} />
          <Route path="/accounts/:accountId" element={<AccountDetail />} />
          <Route path="/accounts/:accountId/canvas" element={<JourneyCanvas />} />
          <Route
            path="/accounts/:accountId/graph"
            element={
              <Suspense fallback={<p className="text-sm text-slate-400">Loading the graph…</p>}>
                <AccountGraph />
              </Suspense>
            }
          />
          <Route path="/platform" element={<Platform />} />
          <Route path="/platform/tenants/:customerId" element={<PlatformTenant />} />
          <Route path="/platform/onboarding" element={<PlatformOnboarding />} />
          <Route path="/platform/onboarding/:customerId" element={<PlatformOnboardingTenant />} />
          <Route path="/" element={<Home />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
