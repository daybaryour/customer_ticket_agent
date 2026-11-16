import { useCallback, useEffect, useMemo, useState } from 'react'
import KnowledgeBase from './KnowledgeBase'
import { API_BASE, errorMessage } from './api'

const TABS = [
  { id: 'pending', label: 'Pending' },
  { id: 'resolved', label: 'Resolved' },
  { id: 'rejected', label: 'Rejected' },
  { id: 'all', label: 'All' },
]

const MANUAL_RESOLUTION_TYPES = ['manual_approved', 'manual_edited', 'manual_written']

const MANUAL_RESOLUTION_LABELS = {
  manual_approved: 'Approved as is',
  manual_edited: 'Edited',
  manual_written: 'Written by agent',
}

function formatConfidence(value) {
  if (value === null || value === undefined) return '—'
  return `${Math.round(Number(value) * 100)}%`
}

function formatDateTime(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString()
}

function resolutionMeta(ticket) {
  const type = ticket?.resolution_type
  if (!type) return null
  if (type === 'auto') return { kind: 'auto', label: 'Auto', detail: '' }
  return { kind: 'manual', label: 'Manual', detail: MANUAL_RESOLUTION_LABELS[type] || type }
}

function matchesResolutionFilter(ticket, filter) {
  if (filter === 'auto') return ticket.resolution_type === 'auto'
  if (filter === 'manual') return MANUAL_RESOLUTION_TYPES.includes(ticket.resolution_type)
  return true
}

function sortTickets(rows) {
  return [...rows].sort(
    (a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id,
  )
}

function normalizeSources(value) {
  if (!Array.isArray(value)) return []
  return value.map((item, index) => {
    if (typeof item === 'string') {
      return { key: `${item}-${index}`, name: item, score: null, text: '' }
    }
    return {
      key: `${item?.source || item?.name || 'source'}-${index}`,
      name: item?.source || item?.name || 'source',
      score: item?.score ?? null,
      text: item?.text || '',
    }
  })
}


function ResolutionBadge({ ticket, showDetail = false }) {
  const meta = resolutionMeta(ticket)
  if (!meta) return null

  return (
    <>
      <span className={`badge badge--resolution-${meta.kind}`}>{meta.label}</span>
      {showDetail && meta.detail && (
        <span className="badge badge--resolution-detail">{meta.detail}</span>
      )}
    </>
  )
}

function StatsBar({ stats }) {
  const items = [
    { label: 'Total tickets', value: stats ? stats.total : '—' },
    { label: 'Pending', value: stats ? stats.pending : '—' },
    { label: 'Auto-resolved', value: stats ? stats.auto_resolved : '—' },
    { label: 'Manually resolved', value: stats ? stats.manual_resolved : '—' },
    {
      label: 'Auto-resolution rate',
      value: stats ? `${(Number(stats.auto_resolution_rate) * 100).toFixed(1)}%` : '—',
    },
  ]

  return (
    <section className="stats-bar" aria-label="Ticket statistics">
      {items.map((item) => (
        <div key={item.label} className="stat">
          <span className="stat__label">{item.label}</span>
          <strong className="stat__value">{item.value}</strong>
        </div>
      ))}
    </section>
  )
}

function TicketListItem({ ticket, selected, onSelect }) {
  return (
    <li>
      <button
        type="button"
        className={`escalation-item${selected ? ' selected' : ''}`}
        onClick={() => onSelect(ticket.id)}
      >
        <span className="escalation-item__topline">
          <strong>#{ticket.id}</strong>
          <span className="mono-time">{formatDateTime(ticket.created_at)}</span>
        </span>
        <span className="escalation-item__subject">
          {ticket.subject || ticket.summary || 'Untitled ticket'}
        </span>
        <span className="badge-row">
          <ResolutionBadge ticket={ticket} showDetail />
          {ticket.status === 'rejected' && <span className="badge badge--rejected">rejected</span>}
          {ticket.urgency && (
            <span className={`badge badge--urgency-${ticket.urgency}`}>{ticket.urgency}</span>
          )}
          {ticket.department && <span className="badge badge--department">{ticket.department}</span>}
          {ticket.sentiment && <span className="badge badge--sentiment">{ticket.sentiment}</span>}
        </span>
      </button>
    </li>
  )
}


function App() {
  const [page, setPage] = useState('tickets')
  const [tickets, setTickets] = useState([])
  const [stats, setStats] = useState(null)
  const [tab, setTab] = useState('pending')
  const [resolvedFilter, setResolvedFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const [selectedDetail, setSelectedDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [draftText, setDraftText] = useState('')

  const [models, setModels] = useState(null)
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [settingsLoading, setSettingsLoading] = useState(true)

  const [testForm, setTestForm] = useState({ subject: '', body: '', tier: '' })
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [actionLoading, setActionLoading] = useState(false)

  const loadTickets = useCallback(async () => {
    const [ticketsResponse, statsResponse] = await Promise.all([
      fetch(`${API_BASE}/tickets`),
      fetch(`${API_BASE}/stats`),
    ])
    if (!ticketsResponse.ok) {
      throw new Error(`Failed to load tickets: ${ticketsResponse.status}`)
    }
    const rows = await ticketsResponse.json()
    setTickets(sortTickets(rows))
    if (statsResponse.ok) setStats(await statsResponse.json())
    return rows
  }, [])

  const loadDetail = useCallback(async (id) => {
    const response = await fetch(`${API_BASE}/tickets/${id}`)
    if (!response.ok) {
      throw new Error(`Failed to load ticket ${id}: ${response.status}`)
    }
    const data = await response.json()
    setSelectedDetail(data)
    setDraftText(data.ai_draft || '')
    return data
  }, [])

  const loadModels = useCallback(async () => {
    setSettingsLoading(true)
    try {
      const [modelsResponse, settingsResponse] = await Promise.all([
        fetch(`${API_BASE}/models`),
        fetch(`${API_BASE}/settings/default`),
      ])

      if (!modelsResponse.ok || !settingsResponse.ok) {
        throw new Error('Unable to load model settings')
      }

      const modelsData = await modelsResponse.json()
      const settingsData = await settingsResponse.json()
      setModels(modelsData)

      const availableProviders = Object.keys(modelsData.available_models || {})
      const initialProvider = availableProviders.includes(settingsData.provider)
        ? settingsData.provider
        : (availableProviders[0] || '')
      const providerModels = modelsData.available_models?.[initialProvider] || []
      const initialModel = providerModels.includes(settingsData.model)
        ? settingsData.model
        : (providerModels[0] || '')

      setProvider(initialProvider)
      setModel(initialModel)
    } catch (err) {
      setError(err.message)
    } finally {
      setSettingsLoading(false)
    }
  }, [])

  useEffect(() => {
    loadTickets().catch((err) => setError(err.message))
    loadModels()

    const interval = window.setInterval(() => {
      loadTickets().catch((err) => setError(err.message))
    }, 10_000)

    return () => window.clearInterval(interval)
  }, [loadTickets, loadModels])

  // Load the full record whenever a new ticket is selected.
  useEffect(() => {
    if (!selectedId) {
      setSelectedDetail(null)
      setDraftText('')
      return
    }

    let cancelled = false
    setDetailLoading(true)
    setError('')

    loadDetail(selectedId)
      .catch((err) => { if (!cancelled) setError(err.message) })
      .finally(() => { if (!cancelled) setDetailLoading(false) })

    return () => { cancelled = true }
  }, [selectedId, loadDetail])

  const selectedBase = useMemo(
    () => tickets.find((item) => item.id === selectedId) || null,
    [tickets, selectedId],
  )
  const selected = selectedDetail?.id === selectedId ? selectedDetail : selectedBase

  const counts = useMemo(() => ({
    pending: tickets.filter((item) => item.status === 'pending').length,
    resolved: tickets.filter((item) => item.status === 'resolved').length,
    rejected: tickets.filter((item) => item.status === 'rejected').length,
    all: tickets.length,
  }), [tickets])

  const visibleTickets = useMemo(() => {
    const rows = tab === 'all' ? tickets : tickets.filter((item) => item.status === tab)
    return tab === 'resolved'
      ? rows.filter((item) => matchesResolutionFilter(item, resolvedFilter))
      : rows
  }, [tickets, tab, resolvedFilter])

  const providers = useMemo(() => Object.keys(models?.available_models || {}), [models])
  const modelOptions = models?.available_models?.[provider] || []


  async function saveModelPref(nextProvider, nextModel) {
    try {
      const response = await fetch(`${API_BASE}/settings/default`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: nextProvider, model: nextModel }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to save model settings: ${response.status}`)
      }
      setNotice(`Saved default model: ${nextProvider}/${nextModel}`)
    } catch (err) {
      setError(err.message)
    }
  }

  function handleProviderChange(event) {
    const nextProvider = event.target.value
    const nextOptions = models?.available_models?.[nextProvider] || []
    const nextModel = nextOptions.includes(model) ? model : (nextOptions[0] || '')
    setProvider(nextProvider)
    setModel(nextModel)
    if (nextProvider && nextModel) saveModelPref(nextProvider, nextModel)
  }

  function handleModelChange(event) {
    const nextModel = event.target.value
    setModel(nextModel)
    if (provider && nextModel) saveModelPref(provider, nextModel)
  }

  async function handleTestTicketSubmit(event) {
    event.preventDefault()
    setActionLoading(true)
    setNotice('')
    setError('')
    try {
      const response = await fetch(`${API_BASE}/test-ticket`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(testForm),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Test ticket failed: ${response.status}`))
      }
      const decisionLabel = data.decision === 'auto_reply'
        ? 'auto-replied'
        : (data.decision === 'escalate' ? 'escalated' : 'processed')
      setNotice(`Test ticket ${decisionLabel}`)
      setTestForm({ subject: '', body: '', tier: '' })

      const updated = await loadTickets()
      const newest = sortTickets(updated)[0]
      if (newest) {
        const nextTab = ['pending', 'resolved', 'rejected'].includes(newest.status)
          ? newest.status
          : 'all'
        setTab(nextTab)
        setResolvedFilter('all')
        setSelectedId(newest.id)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setActionLoading(false)
    }
  }

  async function runAction(id, path, body, buildNotice) {
    setActionLoading(true)
    setNotice('')
    setError('')
    try {
      const response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Request failed: ${response.status}`))
      }
      setNotice(buildNotice(data))
      await loadTickets()
      await loadDetail(id)
      return data
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setActionLoading(false)
    }
  }

  function handleApprove() {
    if (!selected || selected.status !== 'pending') return
    return runAction(
      selected.id,
      `/tickets/${selected.id}/approve`,
      { reply: draftText },
      (data) => {
        const label = MANUAL_RESOLUTION_LABELS[data.resolution_type] || data.resolution_type
        const savedNote = data.saved ? ' and saved to the knowledge base' : ''
        return `Ticket ${selected.id} resolved (${label})${savedNote}`
      },
    )
  }

  function handleReject() {
    if (!selected || selected.status !== 'pending') return
    return runAction(
      selected.id,
      `/tickets/${selected.id}/reject`,
      {},
      () => `Ticket ${selected.id} rejected`,
    )
  }

  function handleResolve() {
    if (!selected || selected.status !== 'pending') return
    if (!draftText.trim()) {
      setError('Write an answer before resolving this ticket.')
      return
    }
    return runAction(
      selected.id,
      `/tickets/${selected.id}/resolve`,
      { answer: draftText },
      (data) => `Ticket ${selected.id} resolved with an agent-written answer${
        data.saved ? ' and saved to the knowledge base' : ''
      }`,
    )
  }

  const confidence = selected?.confidence ?? null
  const issues = selected?.issues || []
  const sources = useMemo(
    () => normalizeSources(
      selectedDetail?.sources_used || selectedDetail?.sources || selectedBase?.sources_used,
    ),
    [selectedDetail, selectedBase],
  )
  const canAct = selected?.status === 'pending'
  const resMeta = resolutionMeta(selected)
  const isEdited = selected?.resolution_type === 'manual_edited'


  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-header__title">
          <h1>Acme Support Tickets</h1>
          <p className="app-header__subtitle">Review AI replies and track each ticket to resolution</p>
        </div>

        <nav className="app-nav" role="tablist" aria-label="Sections">
          <button
            type="button"
            role="tab"
            aria-selected={page === 'tickets'}
            className={`tab${page === 'tickets' ? ' tab--active' : ''}`}
            onClick={() => setPage('tickets')}
          >
            Tickets
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={page === 'kb'}
            className={`tab${page === 'kb' ? ' tab--active' : ''}`}
            onClick={() => setPage('kb')}
          >
            Knowledge base
          </button>
        </nav>

        <div className="model-selector">
          <span className="model-selector__label">Default model</span>
          {settingsLoading ? (
            <span className="muted">Loading…</span>
          ) : (
            <>
              <select value={provider} onChange={handleProviderChange} disabled={!providers.length}>
                {providers.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <select value={model} onChange={handleModelChange} disabled={!modelOptions.length}>
                {modelOptions.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </>
          )}
        </div>

        <form className="test-form" onSubmit={handleTestTicketSubmit}>
          <input
            type="text"
            placeholder="Subject"
            value={testForm.subject}
            onChange={(event) => setTestForm((prev) => ({ ...prev, subject: event.target.value }))}
            required
          />
          <input
            type="text"
            placeholder="Body"
            value={testForm.body}
            onChange={(event) => setTestForm((prev) => ({ ...prev, body: event.target.value }))}
            required
          />
          <select
            value={testForm.tier}
            onChange={(event) => setTestForm((prev) => ({ ...prev, tier: event.target.value }))}
          >
            <option value="">Tier</option>
            <option value="standard">Standard</option>
            <option value="priority">Priority</option>
            <option value="critical">Critical</option>
          </select>
          <button type="submit" disabled={actionLoading}>
            {actionLoading ? 'Submitting…' : 'Submit test ticket'}
          </button>
        </form>
      </header>

      {page === 'tickets' && (
        <>
          <div className="banner-area">
            {notice && <div className="notice">{notice}</div>}
            {error && <div className="error">{error}</div>}
          </div>

          <StatsBar stats={stats} />

      <main className="main">
        <section className="panel panel--list">
          <div className="panel-header">
            <h2>Tickets</h2>
            <span className="count-pill">{visibleTickets.length}</span>
          </div>

          <div className="tabs" role="tablist" aria-label="Ticket status">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={tab === item.id}
                className={`tab${tab === item.id ? ' tab--active' : ''}`}
                onClick={() => setTab(item.id)}
              >
                {item.label}
                <span className="tab__count">{counts[item.id]}</span>
              </button>
            ))}
          </div>

          {tab === 'resolved' && (
            <div className="list-filter">
              <label htmlFor="resolution-filter">Resolution</label>
              <select
                id="resolution-filter"
                value={resolvedFilter}
                onChange={(event) => setResolvedFilter(event.target.value)}
              >
                <option value="all">All</option>
                <option value="auto">Auto</option>
                <option value="manual">Manual</option>
              </select>
            </div>
          )}

          <div className="panel-body">
            {visibleTickets.length === 0 ? (
              <p className="empty-state">No {tab === 'all' ? '' : `${tab} `}tickets to show.</p>
            ) : (
              <ul className="escalation-list">
                {visibleTickets.map((item) => (
                  <TicketListItem
                    key={item.id}
                    ticket={item}
                    selected={selectedId === item.id}
                    onSelect={setSelectedId}
                  />
                ))}
              </ul>
            )}
          </div>
        </section>


        <section className="panel panel--detail">
          {!selected ? (
            <div className="panel-body empty-state">Select a ticket to review.</div>
          ) : (
            <>
              <div className="detail-header">
                <div>
                  <h2>{selected.subject || selected.summary || 'Untitled ticket'}</h2>
                  <div className="detail-meta">
                    <span>Ticket #{selected.id}</span>
                    <span className={`status status--${selected.status}`}>{selected.status}</span>
                    {resMeta && (
                      <span className={`status status--resolution-${resMeta.kind}`}>
                        {resMeta.label}
                      </span>
                    )}
                    <span className="muted">{formatDateTime(selected.created_at)}</span>
                  </div>
                </div>
                {detailLoading && <span className="muted">Refreshing…</span>}
              </div>

              <div className="detail-scroll">
                <div className="detail-block">
                  <h3>Customer ticket</h3>
                  <p className="ticket-body">{selected.body || 'No ticket body provided.'}</p>
                  {(selected.customer_tier || selected.ticket_id) && (
                    <p className="ticket-meta">
                      {selected.customer_tier && <span>Tier: {selected.customer_tier}</span>}
                      {selected.ticket_id && <span>External id: {selected.ticket_id}</span>}
                    </p>
                  )}
                </div>

                {(selected.status === 'resolved' || selected.status === 'rejected') && (
                  <div className="detail-block">
                    <h3>{selected.status === 'resolved' ? 'Resolution' : 'Rejection'}</h3>
                    <div className="meta-grid">
                      <div className="meta-item">
                        <span className="meta-label">Outcome</span>
                        <span className="meta-value">
                          {resMeta ? (
                            <>
                              <ResolutionBadge ticket={selected} />
                              {resMeta.detail && (
                                <span className="meta-hint">{resMeta.detail}</span>
                              )}
                            </>
                          ) : (
                            <span className="muted">No reply sent</span>
                          )}
                        </span>
                      </div>
                      <div className="meta-item">
                        <span className="meta-label">Resolved by</span>
                        <span className="meta-value">{selected.resolved_by || '—'}</span>
                      </div>
                      <div className="meta-item">
                        <span className="meta-label">Resolved at</span>
                        <span className="meta-value">{formatDateTime(selected.resolved_at)}</span>
                      </div>
                      <div className="meta-item">
                        <span className="meta-label">Model used</span>
                        <span className="meta-value mono">{selected.model_used || '—'}</span>
                      </div>
                    </div>

                    {selected.status === 'resolved' && isEdited && (
                      <div className="compare-grid">
                        <div className="answer-column">
                          <h4>Original AI draft</h4>
                          <p className="ticket-body">
                            {selected.ai_draft || 'No AI draft recorded.'}
                          </p>
                        </div>
                        <div className="answer-column">
                          <h4>Final answer sent</h4>
                          <p className="ticket-body">
                            {selected.final_answer || 'No final answer recorded.'}
                          </p>
                        </div>
                      </div>
                    )}

                    {selected.status === 'resolved' && !isEdited && (
                      <div className="answer-column">
                        <h4>Final answer sent</h4>
                        <p className="ticket-body">
                          {selected.final_answer || selected.ai_draft || 'No final answer recorded.'}
                        </p>
                      </div>
                    )}

                    {selected.status === 'rejected' && (
                      <p className="empty-state">This ticket was rejected without sending a reply.</p>
                    )}
                  </div>
                )}


                <div className="detail-block">
                  <h3>Classification</h3>
                  <div className="badge-row">
                    <span className={`badge badge--urgency-${selected.urgency || 'unknown'}`}>
                      Urgency: {selected.urgency || '—'}
                    </span>
                    <span className="badge badge--department">
                      Department: {selected.department || '—'}
                    </span>
                    <span className="badge badge--sentiment">
                      Sentiment: {selected.sentiment || '—'}
                    </span>
                    <span className="badge badge--complexity">
                      Complexity: {selected.complexity || '—'}
                    </span>
                  </div>
                  {selected.summary && <p className="summary">{selected.summary}</p>}
                </div>

                <div className="detail-block">
                  <h3>Confidence</h3>
                  <div className="confidence">
                    <div className="confidence__header">
                      <span>Overall score</span>
                      <strong>{formatConfidence(confidence)}</strong>
                    </div>
                    <div className="confidence__track">
                      <div
                        className="confidence__fill"
                        style={{ width: `${Math.max(0, Math.min(100, Number(confidence || 0) * 100))}%` }}
                      />
                    </div>
                  </div>
                </div>

                <div className="detail-block">
                  <h3>Sources used</h3>
                  {sources.length === 0 ? (
                    <p className="empty-state">No sources were used for this ticket.</p>
                  ) : (
                    <ul className="source-list">
                      {sources.map((source) => (
                        <li key={source.key} className="source-item">
                          <div className="source-item__title">
                            <strong>{source.name}</strong>
                            {source.score !== null && source.score !== undefined && (
                              <span className="mono">score: {Number(source.score).toFixed(4)}</span>
                            )}
                          </div>
                          {source.text && <p className="source-item__text">{source.text}</p>}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div className="detail-block">
                  <h3>Issues</h3>
                  {issues.length === 0 ? (
                    <p className="empty-state">No issues reported.</p>
                  ) : (
                    <ul className="issue-list">
                      {issues.map((issue, index) => <li key={index}>{issue}</li>)}
                    </ul>
                  )}
                </div>

                {canAct && (
                  <div className="detail-block">
                    <h3>Reply draft</h3>
                    <textarea
                      className="draft-textarea"
                      value={draftText}
                      onChange={(event) => setDraftText(event.target.value)}
                      rows={10}
                      placeholder="No draft available — write a reply or resolve with your own answer."
                    />
                    <p className="hint">
                      Approve sends this text as-is (or saves your edits to the knowledge base).
                      Resolve records a written answer without sending it to the customer.
                    </p>
                  </div>
                )}
              </div>

              {canAct ? (
                <div className="action-bar">
                  <button
                    type="button"
                    className="btn btn--approve"
                    disabled={actionLoading || !draftText.trim()}
                    onClick={handleApprove}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="btn btn--reject"
                    disabled={actionLoading}
                    onClick={handleReject}
                  >
                    Reject
                  </button>
                  <button
                    type="button"
                    className="btn btn--resolve"
                    disabled={actionLoading || !draftText.trim()}
                    onClick={handleResolve}
                  >
                    Resolve
                  </button>
                </div>
              ) : (
                <div className="action-bar action-bar--readonly">
                  <span className="muted">
                    Ticket {selected.id} is {selected.status}; no further action is needed.
                  </span>
                </div>
              )}
            </>
          )}
        </section>
      </main>
        </>
      )}

      {page === 'kb' && <KnowledgeBase />}
    </div>
  )
}

export default App

