import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE, errorMessage } from './api'

const ACCEPTED_EXTENSIONS = ['.pdf', '.csv', '.docx', '.txt', '.md']
const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(',')
const MAX_FILE_BYTES = 10 * 1024 * 1024

const TYPE_LABELS = {
  pdf: 'PDF',
  csv: 'CSV',
  docx: 'DOCX',
  txt: 'TXT',
  md: 'MD',
  typed: 'Text',
}

function formatDateTime(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString()
}

function isAcceptedFile(file) {
  const name = (file?.name || '').toLowerCase()
  return ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))
}

function uploadFile(file, uploadedBy, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('files', file)
    form.append('uploaded_by', uploadedBy)

    const request = new XMLHttpRequest()
    request.open('POST', `${API_BASE}/kb/upload`)

    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }

    request.onload = () => {
      let data = {}
      try {
        data = JSON.parse(request.responseText || '{}')
      } catch {
        data = {}
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(data)
      } else {
        reject(new Error(errorMessage(data, `Upload failed: ${request.status}`)))
      }
    }

    request.onerror = () => reject(new Error('Upload failed: network error'))
    request.send(form)
  })
}

function KnowledgeBase() {
  const [documents, setDocuments] = useState([])
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const [uploads, setUploads] = useState([])
  const [dragging, setDragging] = useState(false)

  const [textForm, setTextForm] = useState({ title: '', content: '' })
  const [savingText, setSavingText] = useState(false)

  const [editing, setEditing] = useState(null)
  const [savingEdit, setSavingEdit] = useState(false)
  const [busyId, setBusyId] = useState(null)

  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [searching, setSearching] = useState(false)

  const fileInputRef = useRef(null)

  const loadDocuments = useCallback(async () => {
    const response = await fetch(`${API_BASE}/kb`)
    const data = await response.json().catch(() => [])
    if (!response.ok) {
      throw new Error(errorMessage(data, `Failed to load documents: ${response.status}`))
    }
    setDocuments(Array.isArray(data) ? data : [])
    return data
  }, [])

  useEffect(() => {
    setLoading(true)
    loadDocuments()
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [loadDocuments])

  function updateUpload(key, patch) {
    setUploads((previous) =>
      previous.map((item) => (item.key === key ? { ...item, ...patch } : item)),
    )
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || [])
    if (files.length === 0) return

    setNotice('')
    setError('')

    const entries = files.map((file) => ({
      key: `${file.name}-${file.size}-${Date.now()}-${Math.random()}`,
      file,
      name: file.name,
      status: 'queued',
      progress: 0,
      message: '',
    }))
    setUploads((previous) => [...entries, ...previous])

    let indexedCount = 0

    for (const entry of entries) {
      if (!isAcceptedFile(entry.file)) {
        updateUpload(entry.key, {
          status: 'error',
          progress: 100,
          message: 'Unsupported file type. Use PDF, CSV, DOCX, TXT, or MD.',
        })
        continue
      }
      if (entry.file.size > MAX_FILE_BYTES) {
        updateUpload(entry.key, {
          status: 'error',
          progress: 100,
          message: 'File is larger than 10 MB.',
        })
        continue
      }

      updateUpload(entry.key, { status: 'uploading', progress: 0, message: '' })

      try {
        const data = await uploadFile(entry.file, 'agent', (progress) =>
          updateUpload(entry.key, { progress }),
        )
        const result = data?.results?.[0]
        if (!result) {
          throw new Error('Upload failed: empty response')
        }

        if (result.status === 'indexed') {
          indexedCount += 1
          const chunks = result.chunk_count ?? result.document?.chunk_count ?? 0
          updateUpload(entry.key, {
            status: 'success',
            progress: 100,
            message: `Indexed ${chunks} chunk(s)`,
          })
        } else if (result.status === 'duplicate') {
          updateUpload(entry.key, {
            status: 'duplicate',
            progress: 100,
            message: result.message || 'This file already exists.',
          })
        } else {
          updateUpload(entry.key, {
            status: 'error',
            progress: 100,
            message: result.error || 'Indexing failed.',
          })
        }
      } catch (err) {
        updateUpload(entry.key, {
          status: 'error',
          progress: 100,
          message: err.message,
        })
      }
    }

    try {
      await loadDocuments()
    } catch (err) {
      setError(err.message)
    }

    if (indexedCount > 0) {
      setNotice(
        `Indexed ${indexedCount} file${indexedCount === 1 ? '' : 's'} into the knowledge base.`,
      )
    }
  }

  function handleDrop(event) {
    event.preventDefault()
    setDragging(false)
    handleFiles(event.dataTransfer?.files)
  }

  async function handleTextSubmit(event) {
    event.preventDefault()
    setNotice('')
    setError('')

    const title = textForm.title.trim()
    const content = textForm.content.trim()
    if (!title || !content) {
      setError('Enter both a title and some content.')
      return
    }

    setSavingText(true)
    try {
      const response = await fetch(`${API_BASE}/kb/text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Save failed: ${response.status}`))
      }
      setNotice(`Saved "${data.title}" with ${data.chunk_count} chunk(s).`)
      setTextForm({ title: '', content: '' })
      await loadDocuments()
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingText(false)
    }
  }

  async function handleDelete(doc) {
    const confirmed = window.confirm(
      `Delete "${doc.title}" from the knowledge base? Its chunks will be removed from Chroma.`,
    )
    if (!confirmed) return

    setNotice('')
    setError('')
    setBusyId(doc.id)
    try {
      const response = await fetch(`${API_BASE}/kb/${doc.id}`, { method: 'DELETE' })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Delete failed: ${response.status}`))
      }
      setNotice(`Deleted "${doc.title}".`)
      if (editing?.document?.id === doc.id) setEditing(null)
      await loadDocuments()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyId(null)
    }
  }

  async function openEditor(doc) {
    setNotice('')
    setError('')
    try {
      const response = await fetch(`${API_BASE}/kb/${doc.id}`)
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Failed to load document: ${response.status}`))
      }
      setEditing({
        document: data,
        title: data.title || '',
        content: data.content ?? data.preview ?? '',
        file: null,
      })
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleSaveTextEdit() {
    if (!editing) return
    setSavingEdit(true)
    setNotice('')
    setError('')
    try {
      const response = await fetch(`${API_BASE}/kb/${editing.document.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: editing.title.trim(),
          content: editing.content,
        }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Update failed: ${response.status}`))
      }
      setNotice(`Updated "${data.title}".`)
      setEditing(null)
      await loadDocuments()
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingEdit(false)
    }
  }

  async function handleReplaceFile() {
    if (!editing?.file) return
    setSavingEdit(true)
    setNotice('')
    setError('')
    try {
      const form = new FormData()
      form.append('file', editing.file)
      form.append('title', editing.title.trim())

      const response = await fetch(`${API_BASE}/kb/${editing.document.id}`, {
        method: 'PUT',
        body: form,
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Replace failed: ${response.status}`))
      }
      setNotice(`Replaced the file for "${data.title}".`)
      setEditing(null)
      await loadDocuments()
    } catch (err) {
      setError(err.message)
    } finally {
      setSavingEdit(false)
    }
  }

  async function handleSearch(event) {
    event.preventDefault()
    const query = searchQuery.trim()
    if (!query) return

    setSearching(true)
    setNotice('')
    setError('')
    try {
      const response = await fetch(`${API_BASE}/kb/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(errorMessage(data, `Search failed: ${response.status}`))
      }
      setSearchResults(Array.isArray(data.results) ? data.results : [])
    } catch (err) {
      setError(err.message)
      setSearchResults(null)
    } finally {
      setSearching(false)
    }
  }

  return (
    <div className="kb-page">
      <div className="kb-banners">
        {notice && <div className="notice">{notice}</div>}
        {error && <div className="error">{error}</div>}
      </div>

      <div className="kb-grid">
        <section className="panel kb-panel">
          <div className="panel-header">
            <h2>Upload documents</h2>
          </div>
          <div className="panel-body kb-panel-body">
            <div
              className={`dropzone${dragging ? ' dropzone--active' : ''}`}
              role="button"
              tabIndex={0}
              onClick={() => fileInputRef.current?.click()}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  fileInputRef.current?.click()
                }
              }}
              onDragEnter={(event) => {
                event.preventDefault()
                setDragging(true)
              }}
              onDragOver={(event) => {
                event.preventDefault()
                setDragging(true)
              }}
              onDragLeave={(event) => {
                event.preventDefault()
                setDragging(false)
              }}
              onDrop={handleDrop}
            >
              <p className="dropzone__title">Drag and drop files here</p>
              <p className="hint">PDF, CSV, DOCX, TXT, or MD · up to 10 MB each</p>
              <button
                type="button"
                className="btn btn--primary"
                onClick={(event) => {
                  event.stopPropagation()
                  fileInputRef.current?.click()
                }}
              >
                Choose files
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT_ATTR}
                multiple
                hidden
                onChange={(event) => {
                  handleFiles(event.target.files)
                  event.target.value = ''
                }}
              />
            </div>

            {uploads.length > 0 && (
              <ul className="upload-list">
                {uploads.map((item) => (
                  <li key={item.key} className={`upload-item upload-item--${item.status}`}>
                    <div className="upload-item__row">
                      <span className="upload-item__name">{item.name}</span>
                      <span className="upload-item__status">{item.status}</span>
                    </div>
                    {(item.status === 'uploading' || item.status === 'queued') && (
                      <div className="progress-track">
                        <div className="progress-bar" style={{ width: `${item.progress}%` }} />
                      </div>
                    )}
                    {item.message && <p className="upload-item__message">{item.message}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>

        <section className="panel kb-panel">
          <div className="panel-header">
            <h2>Add text</h2>
          </div>
          <form className="kb-form" onSubmit={handleTextSubmit}>
            <input
              type="text"
              placeholder="Document title"
              value={textForm.title}
              onChange={(event) =>
                setTextForm((previous) => ({ ...previous, title: event.target.value }))
              }
              required
            />
            <textarea
              rows={9}
              placeholder="Paste an FAQ, policy, or troubleshooting notes…"
              value={textForm.content}
              onChange={(event) =>
                setTextForm((previous) => ({ ...previous, content: event.target.value }))
              }
              required
            />
            <button type="submit" className="btn btn--primary" disabled={savingText}>
              {savingText ? 'Saving…' : 'Save to knowledge base'}
            </button>
          </form>
        </section>
      </div>

      {editing && (
        <section className="panel kb-panel kb-editor">
          <div className="panel-header">
            <h2>Edit “{editing.document.title}”</h2>
            <button
              type="button"
              className="kb-close"
              aria-label="Close editor"
              onClick={() => setEditing(null)}
            >
              ×
            </button>
          </div>
          <div className="panel-body kb-panel-body">
            <label className="kb-field">
              <span>Title</span>
              <input
                type="text"
                value={editing.title}
                onChange={(event) =>
                  setEditing((previous) => ({ ...previous, title: event.target.value }))
                }
              />
            </label>

            {editing.document.source_type === 'typed' ? (
              <>
                <label className="kb-field">
                  <span>Content</span>
                  <textarea
                    rows={12}
                    value={editing.content}
                    onChange={(event) =>
                      setEditing((previous) => ({ ...previous, content: event.target.value }))
                    }
                  />
                </label>
                <div className="kb-actions">
                  <button
                    type="button"
                    className="btn btn--primary"
                    onClick={handleSaveTextEdit}
                    disabled={savingEdit || !editing.title.trim() || !editing.content.trim()}
                  >
                    {savingEdit ? 'Saving…' : 'Save changes'}
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="hint">
                  Current file: {editing.document.original_filename || '—'} ·{' '}
                  {editing.document.chunk_count} chunk(s)
                </p>
                <label className="kb-field">
                  <span>Replacement file</span>
                  <input
                    type="file"
                    accept={ACCEPT_ATTR}
                    onChange={(event) =>
                      setEditing((previous) => ({
                        ...previous,
                        file: event.target.files?.[0] || null,
                      }))
                    }
                  />
                </label>
                <div className="kb-actions">
                  <button
                    type="button"
                    className="btn btn--primary"
                    onClick={handleReplaceFile}
                    disabled={savingEdit || !editing.file}
                  >
                    {savingEdit ? 'Replacing…' : 'Replace file'}
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </div>
              </>
            )}
          </div>
        </section>
      )}


      <section className="panel kb-panel">
        <div className="panel-header">
          <h2>Documents</h2>
          <span className="count-pill">{documents.length}</span>
        </div>

        {loading ? (
          <div className="panel-body empty-state">Loading documents…</div>
        ) : documents.length === 0 ? (
          <div className="panel-body empty-state">
            No documents yet. Upload a file or add text to get started.
          </div>
        ) : (
          <div className="kb-table-wrap">
            <table className="kb-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Type</th>
                  <th>Chunks</th>
                  <th>Uploaded by</th>
                  <th>Date</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>
                      <div className="kb-title">{doc.title}</div>
                      {doc.original_filename && doc.original_filename !== doc.title && (
                        <div className="kb-subtitle">{doc.original_filename}</div>
                      )}
                      {doc.status === 'failed' && (
                        <div className="kb-error-text">{doc.error || 'Indexing failed'}</div>
                      )}
                    </td>
                    <td>
                      <span className={`badge badge--kb-${doc.source_type}`}>
                        {TYPE_LABELS[doc.source_type] || doc.source_type}
                      </span>
                    </td>
                    <td className="mono">{doc.status === 'failed' ? '—' : doc.chunk_count}</td>
                    <td>{doc.uploaded_by || '—'}</td>
                    <td className="kb-date">{formatDateTime(doc.created_at)}</td>
                    <td>
                      <div className="kb-actions">
                        <button
                          type="button"
                          className="btn btn--small"
                          onClick={() => openEditor(doc)}
                          disabled={busyId === doc.id}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn btn--small btn--reject"
                          onClick={() => handleDelete(doc)}
                          disabled={busyId === doc.id}
                        >
                          {busyId === doc.id ? 'Deleting…' : 'Delete'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>


      <section className="panel kb-panel">
        <div className="panel-header">
          <h2>Test search</h2>
        </div>
        <form className="kb-search" onSubmit={handleSearch}>
          <input
            type="text"
            placeholder="Ask a question, e.g. how do refunds work?"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
          <button type="submit" className="btn btn--primary" disabled={searching}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>

        {searchResults && (
          <div className="panel-body kb-panel-body">
            {searchResults.length === 0 ? (
              <p className="empty-state">No chunks matched that query.</p>
            ) : (
              <ul className="kb-results">
                {searchResults.map((hit, index) => (
                  <li key={`${hit.doc_id || 'hit'}-${index}`} className="kb-result">
                    <div className="kb-result__head">
                      <strong>{hit.source || hit.title || 'Unknown document'}</strong>
                      <span className="kb-result__meta">
                        {hit.page ? `page ${hit.page} · ` : ''}
                        score: {Number(hit.score ?? 0).toFixed(4)}
                      </span>
                    </div>
                    <p className="kb-result__text">{hit.text}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>
    </div>
  )
}

export default KnowledgeBase

