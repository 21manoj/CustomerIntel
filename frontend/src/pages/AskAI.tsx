import { useEffect, useMemo, useState } from 'react'
import { ApiError, askQuestion, getAskQuestions, getPortfolio } from '../api/client'
import type { AskQuestion, AskResponse, PortfolioRow, Role } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const ROLE_LABEL: Record<string, string> = { admin: 'Admin', cro: 'CRO', cfo: 'CFO', csm: 'CSM' }

function ConfidencePill({ confidence }: { confidence: number | null }) {
  if (confidence == null) return <span className="text-xs text-slate-400">confidence n/a (stub answer)</span>
  const cls = confidence >= 0.7 ? 'bg-emerald-50 text-emerald-700' : confidence >= 0.4 ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700'
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>confidence {Math.round(confidence * 100)}%</span>
}

function AnswerCard({ res }: { res: AskResponse }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-slate-500">
          {res.scope === 'account' ? `Account — ${(res.scope_detail as { account_name?: string }).account_name ?? ''}` : 'Portfolio'}
        </p>
        <div className="flex items-center gap-2">
          <ConfidencePill confidence={res.confidence} />
          <span className="text-xs text-slate-400">{res.model}</span>
        </div>
      </div>
      {res.synthetic && <p className="mt-2 text-xs text-amber-700">{res.disclosure}</p>}
      <p className="mt-3 whitespace-pre-wrap text-sm text-slate-800">{res.answer || '(no sentence could be grounded in the evidence shown)'}</p>

      {res.sentences.some((s) => s.unverified_numbers?.length) && (
        <p className="mt-3 text-xs text-amber-700">
          ⚠ a number in the answer above did not appear in its cited evidence — flagged, not silently trusted.
        </p>
      )}

      {res.evidence_gaps.length > 0 && (
        <div className="mt-4">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-400">What the evidence couldn't say</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-slate-500">
            {res.evidence_gaps.map((g, i) => <li key={i}>{g}</li>)}
          </ul>
        </div>
      )}

      {res.unsupported.length > 0 && (
        <div className="mt-4 rounded-md bg-red-50 p-3">
          <p className="text-xs font-medium text-red-700">{res.unsupported.length} sentence(s) the model tried, dropped for lack of a real citation</p>
          <ul className="mt-1 space-y-1 text-xs text-red-600">
            {res.unsupported.map((u, i) => <li key={i}>"{u.text}" — {u.reason}</li>)}
          </ul>
        </div>
      )}

      <p className="mt-4 text-xs text-slate-400">{Object.keys(res.citations).length} citation(s) · {res.citation_rule}</p>
    </div>
  )
}

export default function AskAI() {
  const { user } = useAuth()
  const [customerId] = useState<number | null>(user?.customer_id ?? null)
  const [questionsByRole, setQuestionsByRole] = useState<Record<string, AskQuestion[]>>({})
  const [availableRoles, setAvailableRoles] = useState<string[]>([])
  const [viewRole, setViewRole] = useState<Role | null>(null)
  const [accounts, setAccounts] = useState<PortfolioRow[]>([])
  const [accountId, setAccountId] = useState<number | ''>('')
  const [freeText, setFreeText] = useState('')
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AskResponse | null>(null)

  useEffect(() => {
    getAskQuestions()
      .then((r) => {
        setQuestionsByRole(r.questions)
        setAvailableRoles(r.roles)
        setViewRole((user?.role as Role) ?? null)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Failed to load suggested questions.'))
  }, [user])

  useEffect(() => {
    if (customerId == null) return
    getPortfolio(customerId).then((r) => setAccounts(r.accounts)).catch(() => {})
  }, [customerId])

  const canSwitchRole = user?.role === 'admin'
  const questions = useMemo(() => (viewRole ? questionsByRole[viewRole] ?? [] : []), [questionsByRole, viewRole])

  async function ask(question: string, requiresAccount: boolean) {
    if (customerId == null || !question.trim()) return
    if (requiresAccount && !accountId) {
      setError('Pick an account first — this question needs one.')
      return
    }
    setAsking(true)
    setError(null)
    setResult(null)
    try {
      const res = await askQuestion(customerId, question, accountId ? Number(accountId) : undefined)
      setResult(res)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to get an answer.')
    } finally {
      setAsking(false)
    }
  }

  if (customerId == null) {
    return <p className="text-sm text-slate-500">No customer context.</p>
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Ask AI</h1>
        <p className="mt-1 text-sm text-slate-500">
          Every sentence below is grounded in cited evidence — a claim that can't cite one is dropped, not guessed.
        </p>
      </div>

      {canSwitchRole && (
        <div className="flex gap-2">
          {availableRoles.map((r) => (
            <button
              key={r}
              onClick={() => setViewRole(r as Role)}
              className={`rounded-full px-3 py-1.5 text-xs font-medium ${
                viewRole === r ? 'bg-slate-900 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {ROLE_LABEL[r] ?? r}
            </button>
          ))}
          <span className="self-center text-xs text-slate-400">admin can preview any role's questions</span>
        </div>
      )}

      <div className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm text-slate-600">
            Account (only needed for account-scoped questions)
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : '')}
              className="w-64 rounded border border-slate-300 px-2 py-1.5 text-sm"
            >
              <option value="">Portfolio-wide</option>
              {accounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>{a.account_name}</option>
              ))}
            </select>
          </label>
        </div>

        <p className="mt-4 text-xs font-medium uppercase tracking-wide text-slate-400">
          Suggested for {ROLE_LABEL[viewRole ?? ''] ?? ''}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {questions.map((q) => (
            <button
              key={q.id}
              onClick={() => ask(q.text, q.scope === 'account')}
              disabled={asking}
              title={q.scope === 'account' ? 'needs an account picked above' : 'runs portfolio-wide'}
              className="rounded-full border border-slate-300 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {q.text}{q.scope === 'account' && ' •'}
            </button>
          ))}
        </div>

        <div className="mt-4 flex gap-2">
          <input
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && ask(freeText, false)}
            placeholder="Or ask your own question…"
            className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            onClick={() => ask(freeText, false)}
            disabled={asking || !freeText.trim()}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {asking ? 'Asking…' : 'Ask'}
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {result && <AnswerCard res={result} />}
    </div>
  )
}
