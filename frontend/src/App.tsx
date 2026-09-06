import { Navigate, Route, Routes } from 'react-router-dom'
import ProtectedRoute from './components/ProtectedRoute'
import Shell from './components/Shell'
import Login from './pages/Login'
import Portfolio from './pages/Portfolio'
import ReviewQueue from './pages/ReviewQueue'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Shell />}>
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/review-queue" element={<ReviewQueue />} />
          <Route path="/" element={<Navigate to="/portfolio" replace />} />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
