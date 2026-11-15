import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BatchEvent,
  type BulkEvent,
  type ClearJobSummary,
  type ClearJobListResponse,
  type ClearOutcomeView,
  type RemovalField,
  type SelectionKind,
} from '../api/client'
import { ErrorNote } from '../components/ui'
import { EMPTY_FACETS, SelectionFilters } from '../components/facet_controls'

const KINDS: { value: SelectionKind; label: string }[] = [
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'song', label: 'Songs' },
]

const FIELDS: RemovalField[] = ['Tags', 'Genres']

interface Target {
  item_id: string
  name: string
  applicable: boolean
  skipped_reason: string | null
  outcomes: ClearOutcomeView[]
}

const EMPTY_FORM = {
  kind: 'artist' as SelectionKind,
  // Lists are the only safe input format here: a comma is a legal character in a value,
  // so one-per-line is the reading nobody has to guess at (ADR 0015).
  keep: '',
  fields: ['Tags'] as RemovalField[],
  limit: 100,
  search: '',
  facets: EMPTY_FACETS,
}

function parseKeep(raw: string): string[] {
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * Clear tags in bulk, keeping a named list.
 *
 * This is the most destructive screen in the app: with an empty keep list it removes every
 * value of the chosen fields from every selected item. Everything about the layout is
 * chosen to make that visible before the write rather than after —
 *
 * - the keep list is shown back as count and values *above* the review button, because
 *   "nothing kept" is a decision, not an absence;
 * - the review table shows exactly what disappears per item, and the summary counts values
 *   removed across the whole batch;
 * - the apply is a second, separate action whose label states the item count;
 * - every write is snapshotted, so the run reverts as one batch.
 *
 * The server enforces the ordering (an apply must present a job id from a diff), so this
 * screen cannot skip the review even if it were rewritten to try.
 */
export function RemoveAllTags() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(EMPTY_FORM)
  const [reviewed, setReviewed] = useState<Target[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [batchId, setBatchId] = useState<string | null>(null)
  const [emptied, setEmptied] = useState<string[]>([])
  const [unmatched, setUnmatched] = useState<string[]>([])
  const [truncated, setTruncated] = useState(false)
  const [removedValues, setRemovedValues] = useState(0)
  const [diffing, setDiffing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [applied, setApplied] = useState<BulkEvent[]>([])
  const [reverted, setReverted] = useState<BulkEvent[]>([])
  const abort = useRef<AbortController | null>(null)

  const jobs = useQuery({
    queryKey: ['clear-jobs'],
    queryFn: () => api.get<ClearJobListResponse>('/api/bulk/remove-all-tags/jobs'),
  })

  const keep = useMemo(() => parseKeep(form.keep), [form.keep])
  const applicable = (reviewed ?? []).filter((item) => item.applicable).length

  function body() {
    return {
      selection: {
        kind: form.kind,
        limit: form.limit,
        search: form.search.trim() || null,
        artist_ids: form.facets.artistIds,
        album_ids: form.facets.albumIds,
        exclude: form.facets.patterns,
      },
      exceptions: keep,
      fields: form.fields,
    }
  }

  async function runDiff() {
    setError(null)
    setReviewed([])
    setApplied([])
    setReverted([])
    setEmptied([])
    setUnmatched([])
    setTruncated(false)
    setRemovedValues(0)
    setJobId(null)
    setBatchId(null)
    setDiffing(true)
    abort.current = new AbortController()
    const collected: Target[] = []
    try {
      await api.stream<BatchEvent>(
        '/api/bulk/remove-all-tags/diff',
        body(),
        (event) => {
          if (event.type === 'item' && 'removals' in event) {
            collected.push({
              item_id: event.item_id,
              name: event.name,
              applicable: event.applicable,
              skipped_reason: event.skipped_reason,
              outcomes: (event.removals ?? []) as ClearOutcomeView[],
            })
            setReviewed([...collected])
          } else if (event.type === 'summary' && 'keeping' in event) {
            setJobId(event.job_id ?? null)
            setBatchId(event.batch_id ?? null)
            setEmptied((event.emptied ?? []).map((entry) => `${entry.name} (${entry.field})`))
            setUnmatched(event.unmatched_exceptions ?? [])
            setTruncated(Boolean(event.truncated))
            setRemovedValues(event.removed_values ?? 0)
          } else if (event.type === 'error') {
            setError(new Error(event.message))
          }
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setDiffing(false)
      void queryClient.invalidateQueries({ queryKey: ['clear-jobs'] })
    }
  }

  async function runApply() {
    if (!jobId) return
    setError(null)
    setApplied([])
    setApplying(true)
    abort.current = new AbortController()
    try {
      await api.stream<BatchEvent>(
        '/api/bulk/remove-all-tags/apply',
        { job_id: jobId, confirm: true },
        (event) => {
          if (event.type === 'summary' && 'clearing' in event && event.batch_id) {
            setBatchId(event.batch_id)
          }
          if (event.type !== 'item' && event.type !== 'done') {
            setApplied((current) => [...current, event as BulkEvent])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setApplying(false)
      void queryClient.invalidateQueries({ queryKey: ['clear-jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  async function runRevert() {
    if (!batchId) return
    setError(null)
    setReverted([])
    abort.current = new AbortController()
    try {
      await api.stream<BatchEvent>(
        `/api/bulk/${batchId}/revert?confirm=true`,
        {},
        (event) => {
          if (event.type !== 'item' && event.type !== 'done') {
            setReverted((current) => [...current, event as BulkEvent])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  const clearingEverything = keep.length === 0

  return (
    <section>
      <h2>Remove all tags, keeping exceptions</h2>
      <p className="muted small">
        Clears every value in the chosen fields from the selected items, except the ones you
        list. Review first: the reviewed set becomes a job, and only that job can be applied.
        Every write is snapshotted, so the whole run reverts as a batch.
      </p>

      <div className="panel">
        <div className="row">
          <div className="pill-row">
            {KINDS.map((entry) => (
              <button
                key={entry.value}
                className={entry.value === form.kind ? 'pill active' : 'pill'}
                onClick={() => setForm({ ...form, kind: entry.value })}
              >
                {entry.label}
              </button>
            ))}
          </div>
        </div>

        <div className="row">
          {FIELDS.map((field) => (
            <label className="check" key={field}>
              <input
                type="checkbox"
                checked={form.fields.includes(field)}
                onChange={() =>
                  setForm({
                    ...form,
                    fields: form.fields.includes(field)
                      ? form.fields.filter((entry) => entry !== field)
                      : [...form.fields, field],
                  })
                }
              />
              {field}
            </label>
          ))}
          <label className="check">
            search
            <input
              value={form.search}
              placeholder="optional"
              onChange={(event) => setForm({ ...form, search: event.target.value })}
              style={{ width: '9rem' }}
            />
          </label>
          <label className="check">
            limit
            <input
              type="number"
              min={1}
              max={500}
              value={form.limit}
              onChange={(event) => setForm({ ...form, limit: Number(event.target.value) })}
              style={{ width: '5rem' }}
            />
          </label>
        </div>

        <div className="row" style={{ alignItems: 'flex-start' }}>
          <label className="check" htmlFor="keep-list">
            keep these (one per line)
          </label>
          <textarea
            id="keep-list"
            value={form.keep}
            rows={4}
            placeholder={'e.g.\nChile'}
            onChange={(event) => setForm({ ...form, keep: event.target.value })}
            style={{ minWidth: '18rem', flex: 1 }}
          />
        </div>

        <SelectionFilters
          kind={form.kind}
          values={form.facets}
          onChange={(facets) => setForm({ ...form, facets })}
          idPrefix="clear"
        />

        <div className="row">
          <button
            className={clearingEverything ? 'danger' : 'primary'}
            onClick={runDiff}
            disabled={diffing || form.fields.length === 0}
          >
            {diffing ? 'Reviewing…' : cleaningLabel(clearingEverything, keep.length)}
          </button>
          <span className="muted small">
            {clearingEverything ? (
              <>
                No exceptions: every value in the chosen fields is removed. Matching is exact
                and case-insensitive — <span className="mono">Chile</span> is kept,{' '}
                <span className="mono">Chilean</span> is not.
              </>
            ) : (
              <>
                Keeping {keep.length} value(s):{' '}
                <span className="chips">
                  {keep.map((value) => (
                    <span className="chip" key={value}>
                      {value}
                    </span>
                  ))}
                </span>
              </>
            )}
          </span>
        </div>
      </div>

      <ErrorNote error={error} />

      {unmatched.length > 0 && (
        <p className="badge warn">
          These exceptions match nothing in the selection, so they keep nothing:{' '}
          {unmatched.map((value) => `“${value}”`).join(', ')}. Check the spelling — matching is
          exact, not a substring.
        </p>
      )}

      {truncated && (
        <p className="badge warn">
          The scan stopped at its cap, so this batch covers only part of the library. Narrow
          the selection (a search term or an exclusion) and run it again.
        </p>
      )}

      {emptied.length > 0 && (
        <p className="badge warn">
          This would leave a field empty on {emptied.length} item(s): {emptied.join(', ')}.
          Legitimate, and revertible, but worth a look before writing.
        </p>
      )}

      {reviewed && (
        <div className="panel">
          <h3>
            Reviewed{' '}
            <span className="muted small">
              {reviewed.length} matched · {applicable} would change · {removedValues} value(s)
              removed
            </span>
          </h3>

          {reviewed.length === 0 ? (
            <p className="badge warn">
              Nothing in this selection carries a value in{' '}
              <span className="mono">{form.fields.join(', ') || '—'}</span>, so there is
              nothing to clear.
            </p>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Field</th>
                  <th>Removing</th>
                  <th>Keeping</th>
                </tr>
              </thead>
              <tbody>
                {reviewed.map((item) =>
                  item.outcomes.map((outcome, index) => (
                    <tr key={`${item.item_id}-${outcome.field}`}>
                      <td>
                        {index === 0 ? <Link to={`/edit/${item.item_id}`}>{item.name}</Link> : ''}
                      </td>
                      <td className="mono">{outcome.field}</td>
                      <td>
                        {outcome.removed.length === 0 ? (
                          <span className="muted">— nothing —</span>
                        ) : (
                          <span className="chips">
                            {outcome.removed.map((value) => (
                              <span className="chip danger-chip" key={value}>
                                {value}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td>
                        {outcome.after.length === 0 ? (
                          <span className="badge warn">emptied</span>
                        ) : (
                          <span className="chips">
                            {outcome.after.map((value) => (
                              <span className="chip" key={value}>
                                {value}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          )}

          {reviewed.length > 0 && (
            <div className="actions">
              <button
                className="danger"
                onClick={runApply}
                disabled={applying || !jobId || applicable === 0}
              >
                {applying ? 'Applying…' : `Clear ${applicable} item(s)`}
              </button>
              <span className="muted small">
                A failure on one item does not stop the rest, and everything applied shares a
                batch you can revert as a unit.
              </span>
            </div>
          )}
        </div>
      )}

      <ErrorNote error={jobs.error} />
      {jobs.data && jobs.data.jobs.length > 0 && (
        <div className="panel">
          <h3>
            Recent runs <span className="muted small">{jobs.data.count}</span>
          </h3>
          <table className="grid">
            <thead>
              <tr>
                <th>Batch</th>
                <th>Clearing</th>
                <th>Keeping</th>
                <th>Values removed</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.jobs.map((job: ClearJobSummary) => (
                <tr key={job.job_id}>
                  <td className="mono small">{job.batch_id.slice(0, 8)}</td>
                  <td>{(job.clearing ?? []).join(', ')}</td>
                  <td>
                    {(job.keeping ?? []).length === 0 ? (
                      <span className="muted">— nothing —</span>
                    ) : (
                      (job.keeping ?? []).join(', ')
                    )}
                  </td>
                  <td>{job.removed_values ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {applied.length > 0 && (
        <div className="panel">
          <h3>Applied</h3>
          <pre className="log">{JSON.stringify(applied, null, 2)}</pre>
          {batchId && (
            <button onClick={runRevert}>Revert this batch</button>
          )}
        </div>
      )}

      {reverted.length > 0 && (
        <div className="panel">
          <h3>Reverted</h3>
          <pre className="log">{JSON.stringify(reverted, null, 2)}</pre>
        </div>
      )}
    </section>
  )
}

function cleaningLabel(clearingEverything: boolean, kept: number): string {
  if (clearingEverything) return 'Review — clear everything'
  return `Review — clearing all but ${kept}`
}
