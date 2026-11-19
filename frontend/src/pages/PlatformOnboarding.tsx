// Onboarding a tenant from the platform console: the tenant and its admin, the files, the
// pre-flight check, the load, and the settings behind the realized retention figure.
// Operators drive it; viewers can follow where a tenant is. The server enforces both.
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'
import * as api from '../api/client'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import type {
  OnboardingCreated,
  OnboardingFileResult,
  OnboardingFileSpec,
  OnboardingOptions,
  OnboardingPreflight,
  OnboardingProcessResult,
  OnboardingStatus,
  RetentionField,
  RetentionResult,
  RetentionSettings,
} from '../api/types'

const INPUT = 'w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none disabled:bg-slate-50'
const PRIMARY = 'rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50'
const SECONDARY = 'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50'
const CARD = 'rounded-lg border border-slate-200 bg-white p-4'

const STEPS = [
  { id: 'tenant', label: 'Tenant' },
  { id: 'files', label: 'Files' },
  { id: 'check', label: 'Check' },
  { id: 'load', label: 'Load' },
  { id: 'retention', label: 'Retention' },
] as const
type StepId = (typeof STEPS)[number]['id']

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong. Try again.'
}

function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function money(n: number | null | undefined): string {
  if (n == null) return '—'
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function pct(n: number | null | undefined): string {
  return n == null ? 'Withheld' : `${(n * 100).toFixed(1)}%`
}

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  )
}

function Stepper({ current, reached, onPick }: { current: StepId; reached: (s: StepId) => boolean; onPick?: (s: StepId) => void }) {
  return (
    <ol className="mb-6 flex flex-wrap gap-2">
      {STEPS.map((s, i) => {
        const active = s.id === current
        const open = reached(s.id) && !!onPick
        return (
          <li key={s.id}>
            <button
              type="button"
              disabled={!open}
              onClick={() => onPick?.(s.id)}
              aria-current={active ? 'step' : undefined}
              className={`flex items-center gap-2 rounded-full border px-3 py-1 text-sm ${
                active
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : open
                    ? 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50'
                    : 'border-slate-200 bg-slate-50 text-slate-400'
              }`}
            >
              <span className="text-xs font-semibold">{i + 1}</span>
              {s.label}
            </button>
          </li>
        )
      })}
    </ol>
  )
}

function Secret({ label, value, note }: { label: string; value: string; note: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <code className="min-w-0 break-all rounded bg-slate-100 px-2 py-1 text-xs text-slate-900">{value}</code>
        <button
          type="button"
          className={SECONDARY}
          onClick={() => {
            navigator.clipboard.writeText(value).then(
              () => setCopied(true),
              () => setCopied(false),
            )
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  )
}

// ── Step 1: the tenant ──

function NewTenant({ options }: { options: OnboardingOptions }) {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [domain, setDomain] = useState('')
  const [vertical, setVertical] = useState(options.verticals[0]?.id ?? '')
  const [tier, setTier] = useState('')
  const [origin, setOrigin] = useState(options.default_data_origin ?? options.data_origins[0]?.id ?? '')
  const [adminName, setAdminName] = useState('')
  const [adminEmail, setAdminEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<OnboardingCreated | null>(null)
  const v = options.verticals.find((x) => x.id === vertical)
  const o = options.data_origins.find((x) => x.id === origin)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      setCreated(
        await api.onboardCreateTenant({
          name, domain, vertical, tier: tier || undefined, data_origin: origin, admin_name: adminName, admin_email: adminEmail,
        }),
      )
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  if (created) {
    return (
      <div className={`${CARD} space-y-4`}>
        <div>
          <h2 className="text-lg font-semibold text-slate-900">{created.name} was created</h2>
          <p className="mt-1 text-sm text-slate-600">
            Tenant {created.customer_id} · {created.domain} · {created.vertical} · data: {created.data_origin}
          </p>
        </div>
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          These two values are shown once. Copy them now; the console cannot show them again.
        </p>
        <Secret
          label={`Setup token for ${created.admin_email}`}
          value={created.admin_setup_token}
          note={`The admin sets a password with this on the sign-in page.${
            created.setup_token_expiry_minutes ? ` It expires after ${created.setup_token_expiry_minutes} minutes; a new one can be issued from the tenant page.` : ''
          }`}
        />
        {created.api_key && <Secret label="API key" value={created.api_key} note="For the tenant's integrations. Send it in the Authorization header." />}
        <button className={PRIMARY} onClick={() => navigate(`/platform/onboarding/${created.customer_id}`)}>
          I have copied them. Continue to files
        </button>
      </div>
    )
  }

  if (!options.can_write) {
    return <p className={`${CARD} text-sm text-slate-600`}>Creating a tenant needs the operator role. Viewers can follow an onboarding from the tenant's page.</p>
  }

  return (
    <form onSubmit={submit} className={`${CARD} space-y-4`}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Company name">
          <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Field label="Domain" hint="Like example.com. It identifies the tenant and cannot be reused.">
          <input className={INPUT} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" required />
        </Field>
        <Field label="Vertical" hint={v?.description}>
          <select className={INPUT} value={vertical} onChange={(e) => { setVertical(e.target.value); setTier('') }}>
            {options.verticals.map((x) => (
              <option key={x.id} value={x.id}>{x.id}</option>
            ))}
          </select>
        </Field>
        {v && v.tiers.choices.length > 0 && (
          <Field label="KPI tier" hint={v.tiers.choices.find((t) => t.id === (tier || v.tiers.default))?.description}>
            <select className={INPUT} value={tier} onChange={(e) => setTier(e.target.value)}>
              <option value="">Default{v.tiers.default ? ` (${v.tiers.default})` : ''}</option>
              {v.tiers.choices.map((t) => (
                <option key={t.id} value={t.id}>{t.name}{t.kpi_count ? ` · ${t.kpi_count} KPIs` : ''}</option>
              ))}
            </select>
          </Field>
        )}
        <Field label="Data origin" hint={o?.disclosure ?? (o?.synthetic ? 'Synthetic data is disclosed wherever it is shown.' : undefined)}>
          <select className={INPUT} value={origin} onChange={(e) => setOrigin(e.target.value)}>
            {options.data_origins.map((x) => (
              <option key={x.id} value={x.id}>{x.label}</option>
            ))}
          </select>
        </Field>
      </div>
      <div className="grid gap-4 border-t border-slate-100 pt-4 sm:grid-cols-2">
        <Field label="Admin name">
          <input className={INPUT} value={adminName} onChange={(e) => setAdminName(e.target.value)} required />
        </Field>
        <Field label="Admin email" hint="The tenant's first user. They get a setup token, not a password.">
          <input className={INPUT} type="email" value={adminEmail} onChange={(e) => setAdminEmail(e.target.value)} required />
        </Field>
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <button className={PRIMARY} disabled={busy}>{busy ? 'Creating…' : 'Create tenant'}</button>
    </form>
  )
}

// ── Step 2: the files ──

function FileRow({ spec, state, cid, canAct, maxBytes, onStaged }: {
  spec: OnboardingFileSpec
  state: OnboardingStatus['files'][number] | undefined
  cid: number
  canAct: boolean
  maxBytes: number | null
  onStaged: () => void
}) {
  const [picked, setPicked] = useState<{ name: string; text: string } | null>(null)
  const [result, setResult] = useState<OnboardingFileResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send(text: string, dryRun: boolean) {
    setError(null)
    setBusy(true)
    try {
      const r = await api.onboardFile(cid, spec.file_type, text, dryRun)
      setResult(r)
      if (r.staged) {
        setPicked(null)
        onStaged()
      }
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  async function pick(file: File | undefined) {
    setResult(null)
    setError(null)
    if (!file) return setPicked(null)
    if (maxBytes && file.size > maxBytes) {
      setPicked(null)
      return setError(`This file is ${(file.size / 1e6).toFixed(1)} MB; the console takes up to ${(maxBytes / 1e6).toFixed(0)} MB. Load larger files with scripts/onboard_tenant.py.`)
    }
    const text = await file.text()
    setPicked({ name: file.name, text })
    void send(text, true)
  }

  const valid = result?.status === 'success'
  return (
    <li className={CARD}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="min-w-0">
          <span className="font-medium text-slate-900">{spec.label}</span>{' '}
          <code className="text-xs text-slate-500">{spec.file_type}</code>{' '}
          <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${spec.expected ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-600'}`}>
            {spec.expected ? 'expected' : 'optional'}
          </span>
        </div>
        <span className={`text-xs ${state?.staged ? 'text-emerald-700' : 'text-slate-500'}`}>
          {state?.staged ? `Staged: ${state.row_count ?? 0} rows, ${when(state.staged_at)}` : 'Not staged'}
        </span>
      </div>
      {spec.what && <p className="mt-1 text-sm text-slate-600">{spec.what}</p>}
      <p className="mt-1 break-words text-xs text-slate-500">Required columns: {spec.required_columns.join(', ') || 'none'}</p>
      {canAct && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            type="file"
            accept=".csv,text/csv"
            aria-label={`Choose ${spec.file_type}`}
            className="max-w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border file:border-slate-300 file:bg-white file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-slate-700"
            onChange={(e) => void pick(e.target.files?.[0])}
          />
          {picked && valid && result?.dry_run && (
            <button className={PRIMARY} disabled={busy} onClick={() => void send(picked.text, false)}>
              {busy ? 'Staging…' : state?.staged ? 'Replace staged file' : 'Stage file'}
            </button>
          )}
        </div>
      )}
      {busy && !result && <p className="mt-2 text-xs text-slate-500">Checking…</p>}
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {result && (
        <div className={`mt-2 rounded-md border px-3 py-2 text-sm ${valid ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-red-200 bg-red-50 text-red-800'}`}>
          {valid
            ? result.staged
              ? `Staged ${result.row_count ?? 0} rows.`
              : `${picked?.name ?? 'The file'} reads cleanly: ${result.row_count ?? 0} rows. Nothing is staged until you press Stage file.`
            : result.message || 'The file was not accepted.'}
          {!!result.missing_required?.length && <div className="mt-1">Missing columns: {result.missing_required.join(', ')}</div>}
          {[...(result.errors ?? []), ...(result.warnings ?? [])].slice(0, 8).map((m, i) => (
            <div key={i} className="mt-1 text-xs">{m}</div>
          ))}
        </div>
      )}
    </li>
  )
}

// ── Step 3: the check ──

function Check({ cid, onNext }: { cid: number; onNext: () => void }) {
  const [data, setData] = useState<OnboardingPreflight | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    setError(null)
    setData(null)
    api.getOnboardingPreflight(cid).then(setData).catch((e) => setError(errorText(e)))
  }, [cid])
  useEffect(load, [load])
  if (error) return <p className="text-sm text-red-600">{error}</p>
  if (!data) return <p className="text-sm text-slate-400">Reading the staged files…</p>
  const tone = { error: 'border-red-200 bg-red-50 text-red-800', warning: 'border-amber-200 bg-amber-50 text-amber-900', info: 'border-slate-200 bg-slate-50 text-slate-600' }
  return (
    <div className="space-y-4">
      <div className={`${CARD} ${data.ok ? '' : 'border-red-200'}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-slate-900">{data.ok ? 'The files can be loaded' : `${data.error_count} problem${data.error_count === 1 ? '' : 's'} must be fixed first`}</h2>
          <button className={SECONDARY} onClick={load}>Check again</button>
        </div>
        <p className="mt-1 text-xs text-slate-500">This check only reads. Nothing is written until you load.</p>
        <ul className="mt-3 space-y-1 text-sm text-slate-700">
          {data.summary.map((s, i) => (
            <li key={i}>{s.replace(' Safe to run process_data.', '')}</li>
          ))}
        </ul>
      </div>
      {data.problems.length > 0 && (
        <ul className="space-y-2">
          {data.problems.map((p, i) => (
            <li key={i} className={`rounded-lg border p-3 text-sm ${tone[p.severity] ?? tone.info}`}>
              <span className="text-xs font-semibold uppercase tracking-wide">{p.severity}</span> {p.message}
            </li>
          ))}
          {data.problem_count > data.problems.length && (
            <li className="text-xs text-slate-500">{data.problem_count - data.problems.length} more not shown.</li>
          )}
        </ul>
      )}
      {data.files && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr><th className="px-3 py-2">File</th><th className="px-3 py-2">Rows</th><th className="px-3 py-2">Columns read</th><th className="px-3 py-2">Kept as attributes</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {Object.entries(data.files).map(([name, f]) => (
                <tr key={name}>
                  <td className="px-3 py-1.5 font-medium text-slate-900">{name}</td>
                  <td className="px-3 py-1.5 tabular-nums">{f.rows.toLocaleString()}</td>
                  <td className="px-3 py-1.5 text-xs text-slate-600">{f.columns_read.join(', ')}</td>
                  <td className="px-3 py-1.5 text-xs text-slate-600">{f.columns_kept_as_attributes.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button className={PRIMARY} disabled={!data.ok} onClick={onNext}>Continue to load</button>
    </div>
  )
}

// ── Step 4: the load ──

function Load({ cid, status, canAct, mode, onDone, onNext }: {
  cid: number
  status: OnboardingStatus
  canAct: boolean
  mode: string | undefined
  onDone: () => void
  onNext: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<OnboardingProcessResult | null>(null)
  const running = busy || status.step === 'processing'

  async function run() {
    setError(null)
    setResult(null)
    setBusy(true)
    try {
      setResult(await api.onboardProcess(cid, mode))
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
      onDone()
    }
  }

  const run0 = status.last_run
  return (
    <div className="space-y-4">
      <div className={CARD}>
        <h2 className="text-lg font-semibold text-slate-900">Load the staged files</h2>
        <p className="mt-1 text-sm text-slate-600">
          This ingests the files, scores health, builds each account's journey and runs the hindsight and foresight analyses.
          It does not call a model. Keep this page open until it finishes; a large tenant can take a few minutes.
        </p>
        <p className="mt-2 text-sm text-slate-700">
          {status.pending_files.length
            ? `Waiting to be loaded: ${status.pending_files.join(', ')}.`
            : status.accounts
              ? 'Everything staged has been loaded.'
              : 'Nothing is staged yet.'}
        </p>
        {canAct ? (
          <button className={`${PRIMARY} mt-3`} disabled={running || (!status.pending_files.length && !status.accounts)} onClick={() => void run()}>
            {running ? 'Loading…' : status.accounts && !status.pending_files.length ? 'Load again' : 'Load data'}
          </button>
        ) : (
          <p className="mt-3 text-xs text-slate-500">Loading needs the operator role.</p>
        )}
        {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      </div>
      {result && (
        <div className={`${CARD} ${result.status === 'success' ? 'border-emerald-200' : 'border-amber-200'}`}>
          <h3 className="font-semibold text-slate-900">Run {result.status}{result.duration_s != null ? ` in ${Math.round(result.duration_s)}s` : ''}</h3>
          <p className="mt-1 text-sm text-slate-700">
            {(result.accounts ?? 0).toLocaleString()} accounts · {(result.kpi_measurements ?? 0).toLocaleString()} KPI measurements
          </p>
          {!!result.steps_completed?.length && <p className="mt-1 break-words text-xs text-slate-500">Steps: {result.steps_completed.join(' → ')}</p>}
          {(result.errors ?? []).slice(0, 5).map((e, i) => (
            <p key={i} className="mt-1 break-words text-xs text-red-700">{typeof e === 'string' ? e : JSON.stringify(e)}</p>
          ))}
          {(result.data_warnings ?? []).slice(0, 5).map((e, i) => (
            <p key={i} className="mt-1 break-words text-xs text-amber-800">{typeof e === 'string' ? e : JSON.stringify(e)}</p>
          ))}
        </div>
      )}
      {run0 && (
        <p className="text-sm text-slate-600">
          Last run: <span className="font-medium text-slate-900">{run0.status}</span>, started {when(run0.started_at)}
          {run0.finished_at ? `, finished ${when(run0.finished_at)}` : ''}
          {run0.first_error ? ` · ${run0.first_error}` : ''}
        </p>
      )}
      <button className={PRIMARY} disabled={status.step !== 'done'} onClick={onNext}>Continue to retention settings</button>
    </div>
  )
}

// ── Step 5: the retention figure and its settings ──

function RetentionFigure({ result }: { result: RetentionResult | null }) {
  if (!result) {
    return <p className={`${CARD} text-sm text-slate-600`}>No hindsight run yet. Load the tenant's data, or save with recompute, to see the figure.</p>
  }
  const p = result.portfolio
  const cells: [string, string, string?][] = [
    ['Net revenue retention', pct(p.nrr)],
    ['Gross revenue retention', pct(p.grr)],
    ['Starting revenue', money(p.starting_arr), `${p.cohort_accounts} accounts in the cohort`],
    ['Lost in window', money(p.lost)],
    ['Expansion in window', money(p.expansion)],
    ['Ending revenue', money(p.net_arr), 'Starting, less lost, plus expansion'],
  ]
  return (
    <div className="space-y-3">
      {p.withheld && (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          The retention figure is withheld: {p.withheld_reason}.
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        {cells.map(([label, value, sub]) => (
          <div key={label} className={CARD}>
            <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
            <div className="mt-1 text-xl font-semibold tabular-nums text-slate-900">{value}</div>
            {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
          </div>
        ))}
      </div>
      <dl className="grid gap-x-6 gap-y-1 text-sm text-slate-700 sm:grid-cols-2">
        <div><dt className="inline text-slate-500">Window: </dt><dd className="inline">{result.window.start} to {result.window.end} ({result.window.months} months)</dd></div>
        <div><dt className="inline text-slate-500">New accounts left out: </dt><dd className="inline">{p.new_accounts_excluded}</dd></div>
        <div><dt className="inline text-slate-500">History inside the window: </dt><dd className="inline">{p.observed_months} months</dd></div>
        <div><dt className="inline text-slate-500">Outcomes with an amount: </dt><dd className="inline">{Math.round(p.amount_coverage * 100)}% ({p.outcomes_without_amount} without)</dd></div>
        <div><dt className="inline text-slate-500">Loss or expansion outside the window: </dt><dd className="inline">{p.outcomes_outside_window}</dd></div>
        <div><dt className="inline text-slate-500">Renewals and saves (not counted): </dt><dd className="inline">{money(p.protected_narrative)}</dd></div>
      </dl>
      <p className="text-xs text-slate-500">Basis: {result.basis}. Computed {when(result.generated_at)}.</p>
    </div>
  )
}

function Retention({ cid, canAct }: { cid: number; canAct: boolean }) {
  const [data, setData] = useState<RetentionSettings | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const take = useCallback((d: RetentionSettings) => {
    setData(d)
    setDraft(Object.fromEntries(d.fields.map((f) => [f.key, String(f.value)])))
  }, [])
  useEffect(() => {
    api.getRetentionSettings(cid).then(take).catch((e) => setError(errorText(e)))
  }, [cid, take])

  async function save(values: Record<string, number | string>, done: string) {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      const d = await api.saveRetentionSettings(cid, values, true)
      take(d)
      setNotice(d.recompute && d.recompute.status !== 'completed' ? `Saved. The figure was not recomputed: ${d.recompute.reason ?? d.recompute.status}.` : done)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!data) return
    const values: Record<string, number | string> = {}
    for (const f of data.fields) values[f.key] = f.type === 'choice' ? draft[f.key] : Number(draft[f.key])
    void save(values, 'Saved and recomputed.')
  }

  if (error && !data) return <p className="text-sm text-red-600">{error}</p>
  if (!data) return <p className="text-sm text-slate-400">Loading…</p>
  const dirty = data.fields.some((f) => String(f.value) !== draft[f.key])
  const control = (f: RetentionField) =>
    f.type === 'choice' ? (
      <select className={INPUT} disabled={!canAct} value={draft[f.key]} onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}>
        {(f.choices ?? []).map((c) => (
          <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>
        ))}
      </select>
    ) : (
      <input
        className={INPUT}
        type="number"
        disabled={!canAct}
        min={f.min}
        max={f.max}
        step={f.type === 'float' ? 0.05 : 1}
        value={draft[f.key]}
        onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
        required
      />
    )
  return (
    <div className="space-y-6">
      <section>
        <h2 className="mb-3 text-lg font-semibold text-slate-900">Realized retention</h2>
        <RetentionFigure result={data.result} />
      </section>
      <section>
        <h2 className="mb-1 text-lg font-semibold text-slate-900">Settings for this tenant</h2>
        <p className="mb-3 text-sm text-slate-600">
          Every value starts at the platform default. A value you change is kept for this tenant only; saving recomputes the figure above.
        </p>
        <form onSubmit={submit} className={`${CARD} space-y-4`}>
          <div className="grid gap-4 sm:grid-cols-2">
            {data.fields.map((f) => (
              <Field
                key={f.key}
                label={f.label}
                hint={
                  <>
                    {f.help} Default: {String(f.default).replace(/_/g, ' ')}.
                    {f.overridden && <span className="ml-1 font-medium text-slate-900">Changed for this tenant.</span>}
                  </>
                }
              >
                {control(f)}
              </Field>
            ))}
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          {notice && <p className="text-sm text-emerald-700">{notice}</p>}
          {canAct ? (
            <div className="flex flex-wrap gap-2">
              <button className={PRIMARY} disabled={busy || !dirty}>{busy ? 'Recomputing…' : 'Save and recompute'}</button>
              <button
                type="button"
                className={SECONDARY}
                disabled={busy || !Object.keys(data.overrides).length}
                onClick={() => void save({}, 'Back to the platform defaults; recomputed.')}
              >
                Reset to defaults
              </button>
            </div>
          ) : (
            <p className="text-xs text-slate-500">Changing these needs the operator role.</p>
          )}
        </form>
      </section>
    </div>
  )
}

// ── Pages ──

function Frame({ title, back, children }: { title: string; back: { to: string; label: string }; children: ReactNode }) {
  return (
    <div>
      <Link to={back.to} className="text-sm text-slate-500 hover:text-slate-900">← {back.label}</Link>
      <h1 className="mb-4 mt-2 text-xl font-semibold text-slate-900">{title}</h1>
      {children}
    </div>
  )
}

function useOptions() {
  const [options, setOptions] = useState<OnboardingOptions | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.getOnboardingOptions().then(setOptions).catch((e) => setError(errorText(e)))
  }, [])
  return { options, error }
}

// /platform/onboarding — step 1
export default function PlatformOnboarding() {
  const { user } = useAuth()
  const { options, error } = useOptions()
  if (!user?.platform_role) return <Navigate to="/" replace />
  return (
    <Frame title="Onboard a tenant" back={{ to: '/platform', label: 'Platform console' }}>
      <Stepper current="tenant" reached={() => false} />
      {error && <p className="text-sm text-red-600">{error}</p>}
      {options ? <NewTenant options={options} /> : !error && <p className="text-sm text-slate-400">Loading…</p>}
    </Frame>
  )
}

// /platform/onboarding/:customerId — steps 2 to 5
export function PlatformOnboardingTenant() {
  const { user } = useAuth()
  const { customerId } = useParams()
  const cid = Number(customerId)
  const { options, error: optionsError } = useOptions()
  const [status, setStatus] = useState<OnboardingStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [step, setStep] = useState<StepId | null>(null)

  const reload = useCallback(() => {
    api.getOnboardingStatus(cid).then(
      (s) => {
        setStatus(s)
        setStep((cur) => cur ?? (s.step === 'upload' ? 'files' : s.step === 'check' ? 'check' : s.step === 'processing' ? 'load' : 'retention'))
      },
      (e) => setError(errorText(e)),
    )
  }, [cid])
  useEffect(reload, [reload])

  if (!user?.platform_role) return <Navigate to="/" replace />
  if (error || optionsError) return <p className="text-sm text-red-600">{error ?? optionsError}</p>
  if (!status || !options || !step) return <p className="text-sm text-slate-400">Loading…</p>
  const canAct = options.can_write
  const anyStaged = status.files.some((f) => f.staged)
  const reached = (s: StepId) =>
    s === 'files' || (s === 'check' && anyStaged) || (s === 'load' && (anyStaged || status.accounts > 0)) || (s === 'retention' && status.accounts > 0)
  const t = status.tenant
  const admin = status.admins[0]

  return (
    <Frame title={`Onboarding ${t.name}`} back={{ to: `/platform/tenants/${cid}`, label: `${t.name} in the console` }}>
      <p className="-mt-2 mb-4 text-sm text-slate-600">
        {t.domain} · {t.vertical} · data: {t.data_origin} · {status.accounts.toLocaleString()} accounts loaded
        {admin && ` · admin ${admin.email} ${admin.password_set ? 'has set a password' : 'has not set a password yet'}`}
      </p>
      <Stepper current={step} reached={reached} onPick={setStep} />
      {step === 'files' && (
        <div className="space-y-4">
          {status.missing_expected.length > 0 && (
            <p className="text-sm text-slate-600">Still expected: {status.missing_expected.join(', ')}. Choosing a file checks it first; nothing is staged until you say so.</p>
          )}
          <ul className="space-y-3">
            {options.files.map((f) => (
              <FileRow key={f.file_type} spec={f} state={status.files.find((s) => s.file_type === f.file_type)} cid={cid} canAct={canAct} maxBytes={options.max_csv_bytes} onStaged={reload} />
            ))}
          </ul>
          <button className={PRIMARY} disabled={!anyStaged} onClick={() => setStep('check')}>Continue to check</button>
        </div>
      )}
      {step === 'check' && <Check cid={cid} onNext={() => setStep('load')} />}
      {step === 'load' && <Load cid={cid} status={status} canAct={canAct} mode={options.modes[0]} onDone={reload} onNext={() => setStep('retention')} />}
      {step === 'retention' && <Retention cid={cid} canAct={canAct} />}
    </Frame>
  )
}
