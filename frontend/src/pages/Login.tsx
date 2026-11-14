import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, setPassword as redeemSetupToken } from '../api/client'
import { useAuth } from '../auth/AuthContext'

export default function Login() {
  const { user, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  // 'setup': redeem the one-time token an admin or operator issued, choosing a password.
  const [mode, setMode] = useState<'signin' | 'setup'>('signin')
  const [token, setToken] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  if (user) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      if (mode === 'setup') {
        await redeemSetupToken(token.trim(), password)
        setMode('signin')
        setToken('')
        setPassword('')
        setNotice('Password set. Sign in with your email and the new password.')
        return
      }
      await login(email, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong — try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-8 shadow-sm">
        <h1 className="mb-1 text-lg font-semibold text-slate-900">CustomerIntel</h1>
        <p className="mb-6 text-sm text-slate-500">
          {mode === 'setup' ? 'Set your password with the one-time token you were given' : 'Sign in to your account'}
        </p>
        {notice && <p className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{notice}</p>}
        <form onSubmit={handleSubmit} className="space-y-4">
          {mode === 'signin' ? (
            <div>
              <label htmlFor="email" className="mb-1 block text-sm font-medium text-slate-700">
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
              />
            </div>
          ) : (
            <div>
              <label htmlFor="token" className="mb-1 block text-sm font-medium text-slate-700">
                One-time token
              </label>
              <input
                id="token"
                type="text"
                required
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                className="w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-xs focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
              />
            </div>
          )}
          <div>
            <label htmlFor="password" className="mb-1 block text-sm font-medium text-slate-700">
              {mode === 'setup' ? 'New password' : 'Password'}
            </label>
            <input
              id="password"
              type="password"
              required
              autoComplete={mode === 'setup' ? 'new-password' : 'current-password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            />
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {mode === 'setup' ? (submitting ? 'Saving…' : 'Set password') : submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="mt-6 text-xs text-slate-400">
          No self-service password reset — an admin issues a one-time token out of band.{' '}
          <button
            type="button"
            onClick={() => {
              setMode(mode === 'setup' ? 'signin' : 'setup')
              setError(null)
              setNotice(null)
            }}
            className="font-medium text-slate-600 underline hover:text-slate-900"
          >
            {mode === 'setup' ? 'Back to sign in' : 'I have a token'}
          </button>
        </p>
      </div>
    </div>
  )
}
