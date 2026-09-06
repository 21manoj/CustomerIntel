import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import {
  ApiError,
  configurePlaybooks,
  getPlaybookConfig,
  getUsers,
  inviteUser,
  patchUser,
  resetUserPassword,
} from '../api/client'
import type { PlaybookConfigResponse, Role, SetupTokenResponse, UserView } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const ROLES: Role[] = ['admin', 'cro', 'cfo', 'csm']

// The setup token is shown exactly once in the API response — never persisted, never re-fetchable.
// Surface it as a banner the admin must dismiss, not a toast, with copy-to-clipboard.
function SetupTokenBanner({ token, note, onDismiss }: { token: string; note: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(token)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // clipboard API unavailable — the token is still selectable in the banner
    }
  }
  return (
    <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-4">
      <p className="mb-2 text-sm font-medium text-amber-900">{note}</p>
      <div className="flex items-center gap-2">
        <code className="flex-1 select-all break-all rounded bg-white px-2 py-1 text-xs text-amber-950">{token}</code>
        <button
          onClick={copy}
          type="button"
          className="rounded-md border border-amber-300 bg-white px-2 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button
          onClick={onDismiss}
          type="button"
          className="rounded-md px-2 py-1 text-xs font-medium text-amber-700 hover:text-amber-900"
        >
          Dismiss
        </button>
      </div>
      <p className="mt-2 text-xs text-amber-700">This will not be shown again — relay it to the user out of band now.</p>
    </div>
  )
}

function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null
  return <p className="mt-2 text-sm text-red-600">{message}</p>
}

function errMsg(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}

// ── Users section ─────────────────────────────────────────────────────

function UsersSection({ customerId }: { customerId: number }) {
  const { user: me } = useAuth()
  const [users, setUsers] = useState<UserView[]>([])
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [token, setToken] = useState<SetupTokenResponse | null>(null)

  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteName, setInviteName] = useState('')
  const [inviteRole, setInviteRole] = useState<Role>('csm')
  const [inviteError, setInviteError] = useState<string | null>(null)
  const [inviting, setInviting] = useState(false)

  const [rowError, setRowError] = useState<{ id: number; message: string } | null>(null)
  const [busyRow, setBusyRow] = useState<number | null>(null)

  function load() {
    setLoading(true)
    setListError(null)
    getUsers(customerId)
      .then((res) => setUsers(res.users))
      .catch((err) => setListError(errMsg(err, 'Failed to load users.')))
      .finally(() => setLoading(false))
  }

  useEffect(load, [customerId])

  async function handleInvite(e: FormEvent) {
    e.preventDefault()
    setInviteError(null)
    setInviting(true)
    try {
      const res = await inviteUser({ customer_id: customerId, email: inviteEmail, name: inviteName, role: inviteRole })
      setToken({ setup_token: res.setup_token, setup_token_note: res.setup_token_note })
      setInviteEmail('')
      setInviteName('')
      setInviteRole('csm')
      load()
    } catch (err) {
      setInviteError(errMsg(err, 'Failed to invite user.'))
    } finally {
      setInviting(false)
    }
  }

  async function handleRoleChange(u: UserView, role: Role) {
    setRowError(null)
    setBusyRow(u.user_id)
    try {
      const updated = await patchUser(u.user_id, { role })
      setUsers((prev) => prev.map((row) => (row.user_id === updated.user_id ? updated : row)))
    } catch (err) {
      setRowError({ id: u.user_id, message: errMsg(err, 'Failed to update role.') })
    } finally {
      setBusyRow(null)
    }
  }

  async function handleActiveToggle(u: UserView) {
    setRowError(null)
    setBusyRow(u.user_id)
    try {
      const updated = await patchUser(u.user_id, { active: !u.active })
      setUsers((prev) => prev.map((row) => (row.user_id === updated.user_id ? updated : row)))
    } catch (err) {
      setRowError({ id: u.user_id, message: errMsg(err, 'Failed to update status.') })
    } finally {
      setBusyRow(null)
    }
  }

  async function handleResetPassword(u: UserView) {
    setRowError(null)
    setBusyRow(u.user_id)
    try {
      const res = await resetUserPassword(u.user_id)
      setToken(res)
    } catch (err) {
      setRowError({ id: u.user_id, message: errMsg(err, 'Failed to reset password.') })
    } finally {
      setBusyRow(null)
    }
  }

  return (
    <section className="mb-10">
      <h2 className="mb-3 text-lg font-semibold text-slate-900">Users</h2>

      {token && <SetupTokenBanner token={token.setup_token} note={token.setup_token_note} onDismiss={() => setToken(null)} />}

      <form onSubmit={handleInvite} className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Email</label>
          <input
            type="email"
            required
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            className="w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Name</label>
          <input
            type="text"
            required
            value={inviteName}
            onChange={(e) => setInviteName(e.target.value)}
            className="w-44 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Role</label>
          <select
            value={inviteRole}
            onChange={(e) => setInviteRole(e.target.value as Role)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          disabled={inviting}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {inviting ? 'Inviting…' : 'Invite user'}
        </button>
        <ErrorNote message={inviteError} />
      </form>

      {loading && <p className="text-sm text-slate-400">Loading…</p>}
      {listError && <p className="text-sm text-red-600">{listError}</p>}

      {!loading && !listError && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Email</th>
                <th className="px-4 py-2">Role</th>
                <th className="px-4 py-2">Active</th>
                <th className="px-4 py-2">Last login</th>
                <th className="px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {users.map((u) => {
                const isSelf = u.user_id === me?.user_id
                const busy = busyRow === u.user_id
                return (
                  <tr key={u.user_id} className="hover:bg-slate-50">
                    <td className="px-4 py-3 font-medium text-slate-900">{u.name ?? '—'}</td>
                    <td className="px-4 py-3 text-slate-600">{u.email}</td>
                    <td className="px-4 py-3">
                      <select
                        value={u.role}
                        disabled={busy}
                        onChange={(e) => handleRoleChange(u, e.target.value as Role)}
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs"
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-3">
                      <label className="inline-flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={u.active}
                          disabled={busy || isSelf}
                          title={isSelf ? 'You cannot deactivate your own account' : undefined}
                          onChange={() => handleActiveToggle(u)}
                        />
                        <span className="text-xs text-slate-500">{u.active ? 'active' : 'inactive'}</span>
                      </label>
                    </td>
                    <td className="px-4 py-3 text-slate-500">{u.last_login ?? 'never'}</td>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => handleResetPassword(u)}
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                      >
                        Reset password
                      </button>
                      {rowError?.id === u.user_id && <p className="mt-1 text-xs text-red-600">{rowError.message}</p>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ── Playbook config section ──────────────────────────────────────────

function hostOf(url: string | null): string {
  if (!url) return 'not set'
  try {
    return new URL(url).host
  } catch {
    return 'set (unparsable URL)'
  }
}

function PlaybookSection({ customerId }: { customerId: number }) {
  const [data, setData] = useState<PlaybookConfigResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [webhookUrlInput, setWebhookUrlInput] = useState('')
  const [webhookSecretInput, setWebhookSecretInput] = useState('')
  const [clearWebhook, setClearWebhook] = useState(false)
  const [webhookSaving, setWebhookSaving] = useState(false)
  const [webhookError, setWebhookError] = useState<string | null>(null)
  const [webhookSaved, setWebhookSaved] = useState(false)

  const [disabledSet, setDisabledSet] = useState<Set<string>>(new Set())
  const [disabledSaving, setDisabledSaving] = useState(false)
  const [disabledError, setDisabledError] = useState<string | null>(null)

  const [automationLevel, setAutomationLevel] = useState(0)
  const [killSwitch, setKillSwitch] = useState(false)
  const [govSaving, setGovSaving] = useState(false)
  const [govError, setGovError] = useState<string | null>(null)

  function load() {
    setLoading(true)
    setLoadError(null)
    getPlaybookConfig(customerId)
      .then((res) => {
        setData(res)
        setDisabledSet(new Set(res.disabled))
        setAutomationLevel(res.tenant.automation_level)
        setKillSwitch(res.tenant.kill_switch)
      })
      .catch((err) => setLoadError(errMsg(err, 'Failed to load playbook config.')))
      .finally(() => setLoading(false))
  }

  useEffect(load, [customerId])

  // The vertical's full catalog (playbooks_for_customer already strips disabled ones out of
  // `playbooks`, so the disabled ids only carry their id — no label/metadata is returned for them).
  const allPlaybooks = useMemo(() => {
    if (!data) return []
    const known = data.playbooks.map((p) => ({ id: p.id, label: p.label }))
    const knownIds = new Set(known.map((p) => p.id))
    const extra = data.disabled.filter((id) => !knownIds.has(id)).map((id) => ({ id, label: id }))
    return [...known, ...extra].sort((a, b) => a.id.localeCompare(b.id))
  }, [data])

  async function handleWebhookSave(e: FormEvent) {
    e.preventDefault()
    setWebhookError(null)
    setWebhookSaving(true)
    setWebhookSaved(false)
    try {
      const payload: Parameters<typeof configurePlaybooks>[0] = { customer_id: customerId }
      if (clearWebhook) {
        payload.webhook_url = ''
        payload.webhook_secret = ''
      } else {
        if (webhookUrlInput.trim()) payload.webhook_url = webhookUrlInput.trim()
        if (webhookSecretInput.trim()) payload.webhook_secret = webhookSecretInput.trim()
      }
      await configurePlaybooks(payload)
      setWebhookUrlInput('')
      setWebhookSecretInput('')
      setClearWebhook(false)
      setWebhookSaved(true)
      load()
    } catch (err) {
      setWebhookError(errMsg(err, 'Failed to save webhook config.'))
    } finally {
      setWebhookSaving(false)
    }
  }

  function toggleDisabled(id: string) {
    setDisabledSet((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function handleDisabledSave() {
    setDisabledError(null)
    setDisabledSaving(true)
    try {
      await configurePlaybooks({ customer_id: customerId, disabled_playbooks: Array.from(disabledSet) })
      load()
    } catch (err) {
      setDisabledError(errMsg(err, 'Failed to save disabled playbooks.'))
    } finally {
      setDisabledSaving(false)
    }
  }

  async function handleGovSave() {
    setGovError(null)
    setGovSaving(true)
    try {
      await configurePlaybooks({ customer_id: customerId, automation_level: automationLevel, kill_switch: killSwitch })
      load()
    } catch (err) {
      setGovError(errMsg(err, 'Failed to save governance settings.'))
    } finally {
      setGovSaving(false)
    }
  }

  if (loading && !data) return <p className="text-sm text-slate-400">Loading…</p>
  if (loadError) return <p className="text-sm text-red-600">{loadError}</p>
  if (!data) return null

  return (
    <section>
      <h2 className="mb-3 text-lg font-semibold text-slate-900">Playbook config</h2>
      <p className="mb-4 text-xs text-slate-500">
        Vertical <span className="font-medium text-slate-700">{data.vertical}</span>
        {data.version && <> · catalog v{data.version}</>}
        {data.note && <span className="ml-2 text-amber-600">{data.note}</span>}
      </p>

      {/* Webhook */}
      <div className="mb-4 rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-2 text-sm font-semibold text-slate-800">Delivery webhook</h3>
        <p className="mb-3 text-xs text-slate-500">
          Current target host: <span className="font-mono text-slate-700">{hostOf(data.tenant.webhook_url)}</span> · signing
          secret {data.tenant.webhook_secret_set ? 'configured' : 'not set'}
          <br />
          Only the hostname is shown here — the full URL and secret are write-only from this form.
        </p>
        <form onSubmit={handleWebhookSave} className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600">New webhook URL (https)</label>
            <input
              type="url"
              placeholder="leave blank to keep current"
              value={webhookUrlInput}
              disabled={clearWebhook}
              onChange={(e) => setWebhookUrlInput(e.target.value)}
              className="w-72 rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600">New signing secret</label>
            <input
              type="password"
              placeholder="leave blank to keep current"
              value={webhookSecretInput}
              disabled={clearWebhook}
              onChange={(e) => setWebhookSecretInput(e.target.value)}
              className="w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm disabled:bg-slate-50"
            />
          </div>
          <label className="mb-1.5 flex items-center gap-2 text-xs text-slate-600">
            <input type="checkbox" checked={clearWebhook} onChange={(e) => setClearWebhook(e.target.checked)} />
            Clear webhook (remove URL + secret)
          </label>
          <button
            type="submit"
            disabled={webhookSaving}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {webhookSaving ? 'Saving…' : 'Save'}
          </button>
        </form>
        {webhookSaved && <p className="mt-2 text-sm text-emerald-700">Saved.</p>}
        <ErrorNote message={webhookError} />
        <p className="mt-2 text-xs text-slate-400">
          Blank fields are left unchanged — nothing is wiped unless you check "Clear webhook". A webhook URL requires a
          signing secret to be set (the backend rejects one without the other).
        </p>
      </div>

      {/* Disabled playbooks */}
      <div className="mb-4 rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-2 text-sm font-semibold text-slate-800">Disabled playbooks</h3>
        {allPlaybooks.length === 0 ? (
          <p className="text-sm text-slate-500">No playbooks defined for this vertical.</p>
        ) : (
          <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {allPlaybooks.map((p) => (
              <label key={p.id} className="flex items-center gap-2 text-sm text-slate-700">
                <input type="checkbox" checked={disabledSet.has(p.id)} onChange={() => toggleDisabled(p.id)} />
                {p.label} <span className="text-xs text-slate-400">({p.id})</span>
              </label>
            ))}
          </div>
        )}
        <button
          type="button"
          disabled={disabledSaving || allPlaybooks.length === 0}
          onClick={handleDisabledSave}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {disabledSaving ? 'Saving…' : 'Save disabled playbooks'}
        </button>
        <ErrorNote message={disabledError} />
      </div>

      {/* Automation level + kill switch */}
      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <h3 className="mb-2 text-sm font-semibold text-slate-800">Automation & kill switch</h3>
        <p className="mb-3 text-xs text-slate-500">Current: {data.tenant.automation_level_meaning}</p>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600">Automation level</label>
            <select
              value={automationLevel}
              onChange={(e) => setAutomationLevel(Number(e.target.value))}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            >
              <option value={0}>0 — every approval is human</option>
              <option value={1}>1 — auto-approve notify-class playbooks</option>
            </select>
          </div>
          <label className="mb-1.5 flex items-center gap-2 text-sm text-red-700">
            <input type="checkbox" checked={killSwitch} onChange={(e) => setKillSwitch(e.target.checked)} />
            Kill switch — nothing is approved or sent
          </label>
          <button
            type="button"
            disabled={govSaving}
            onClick={handleGovSave}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {govSaving ? 'Saving…' : 'Save'}
          </button>
        </div>
        {killSwitch && (
          <p className="mt-2 text-xs font-medium text-red-600">
            Warning: with the kill switch on, no playbook action is approved or sent for this tenant, regardless of
            automation level.
          </p>
        )}
        <ErrorNote message={govError} />
      </div>
    </section>
  )
}

// ── Page ──────────────────────────────────────────────────────────────

export default function Settings() {
  const { user } = useAuth()
  const [customerId, setCustomerId] = useState<number | null>(user?.customer_id ?? null)

  if (user?.role !== 'admin') {
    return <Navigate to="/portfolio" replace />
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
        <label className="flex items-center gap-2 text-sm text-slate-500">
          Customer ID
          <input
            type="number"
            value={customerId ?? ''}
            onChange={(e) => setCustomerId(e.target.value ? Number(e.target.value) : null)}
            className="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm"
            placeholder="e.g. 1"
          />
        </label>
      </div>

      {customerId == null ? (
        <p className="text-sm text-slate-500">Enter a customer ID to manage its users and playbook config.</p>
      ) : (
        <>
          <UsersSection customerId={customerId} />
          <PlaybookSection customerId={customerId} />
        </>
      )}
    </div>
  )
}
