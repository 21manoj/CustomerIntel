import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

// `roles`, when present, restricts the link to those SessionUser.role values (checked against the
// logged-in user below) — omit it for a link every role should see. Generic on purpose: other pages
// landing in parallel (Interventions, Review Queue, Calibrations, Settings, ...) need the same gating.
const NAV_ITEMS: { to: string; label: string; roles?: string[] }[] = [
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/ask', label: 'Ask AI' },
  { to: '/interventions', label: 'Interventions' },
  { to: '/activity', label: 'Activity' },
  { to: '/review-queue', label: 'Review Queue', roles: ['csm', 'admin'] },
  { to: '/roi', label: 'ROI & Power-of-1', roles: ['cfo', 'cro', 'admin'] },
  { to: '/settings', label: 'Settings', roles: ['admin'] },
  { to: '/calibrations', label: 'Calibrations', roles: ['admin'] },
  // Account/Journey Canvas — added as its own page lands.
]

export default function Shell() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  // The platform console is gated on platform_role, never on `role` — and someone who belongs to no
  // tenant (a console-only operator) is not offered tenant pages that would only refuse them.
  const tenantItems = user?.customer_id
    ? NAV_ITEMS.filter((item) => !item.roles || (user && item.roles.includes(user.role)))
    : []
  const navItems = user?.platform_role ? [...tenantItems, { to: '/platform', label: 'Platform' }] : tenantItems

  async function handleLogout() {
    await logout()
    navigate('/login')
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-6 py-3">
          <div className="flex shrink-0 items-center gap-8">
            <span className="font-semibold tracking-tight text-slate-900">CustomerIntel</span>
            <nav className="flex gap-4">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `whitespace-nowrap text-sm font-medium ${isActive ? 'text-slate-900' : 'text-slate-500 hover:text-slate-700'}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex min-w-0 items-center gap-3 text-sm">
            <span className="min-w-0 truncate text-slate-500" title={user?.email}>{user?.email}</span>
            <span className="shrink-0 whitespace-nowrap rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-slate-600">
              {user?.platform_role ? `platform ${user.platform_role}` : user?.role}
            </span>
            <button onClick={handleLogout} className="shrink-0 whitespace-nowrap font-medium text-slate-500 hover:text-slate-900">
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}
