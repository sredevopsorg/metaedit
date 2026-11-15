import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BatchEvent,
  type BulkEvent,
  type MergeGroupRequest,
  type RemovalField,
  type SelectionKind,
  type SimilarityGroupView,
  type SimilarityPreviewResponse,
} from '../api/client'
import { ErrorNote } from '../components/ui'
import { EMPTY_FACETS, SelectionFilters } from '../components/facet_controls'

const KINDS: { value: SelectionKind; label: string }[] = [
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'song', label: 'Songs' },
]

const FIELDS: RemovalField[] = ['Genres', 'Tags']

interface Target {
  item_id: string
  name: string
  applicable: boolean
  skipped_reason: string | null
  merge: Record<string, number[]>
}

/**
 * Merge values that are one genre spelled differently.
 *
 * Three deliberate steps, and the first one is the feature's defining constraint: the
 * preview **finds and explains**, it never writes. The user asked for similarity merging,
 * and the answer here is that a guess about what counts as the same genre is a judgement
 * call — so the operator sees the groups, picks which ones to apply, and only then does a
 * reviewed batch rewrite anything.
 *
 * The preview is also the only place the rule is stated. A group of `Hip Hop` + `hip-hop`
 * is shown with the reason (they squash to the same key) rather than as an assertion, so an
 * operator can tell a spelling variant from a real genre they meant to keep apart — which
 * is why `Rock` and `Rockabilly` never appear in the same group and why `Drum & Bass` does
 * not merge with `Drum and Bass`.
 */
export function MergeSimilar() {
  const queryClient = useQueryClient()
  const [kind, setKind] = useState<SelectionKind>('artist')
  const [fields, setFields] = useState<RemovalField[]>(['Genres', 'Tags'])
  const [facets, setFacets] = useState(EMPTY_FACETS)
  const [limit, setLimit] = useState(100)
  const [search, setSearch] = useState('')
  const [keep, setKeep] = useState('')
  const [exceptions, setExceptions] = useState('')

  const [preview, setPreview] = useState<SimilarityPreviewResponse | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [previewing, setPreviewing] = useState(false)

  const [reviewed, setReviewed] = useState<Target[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [batchId, setBatchId] = useState<string | null>(null)
  const [diffing, setDiffing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState<BulkEvent[]>([])
  const [reverted, setReverted] = useState<BulkEvent[]>([])
  const [rewritten, setRewritten] = useState(0)
  const [error, setError] = useState<unknown>(null)

  function lines(raw: string): string[] {
    return raw
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
  }

  async function runPreview() {
    setError(null)
    setPreview(null)
    setSelected(new Set())
    setReviewed(null)
    setJobId(null)
    setBatchId(null)
    setPreviewing(true)
    try {
      const result = await api.post<SimilarityPreviewResponse>(
        '/api/bulk/merge-similar/preview',
        {
          kind,
          fields,
          keep: lines(keep),
          exceptions: lines(exceptions),
        },
      )
      setPreview(result)
      // Everything found is pre-selected: the operator asked for merging, and every group
      // is one the rule derived from a spelling difference. Unticking is the way to be
      // selective, which keeps the common case to one click.
      setSelected(new Set((result.groups ?? []).map((group) => group.squashed)))
    } catch (caught) {
      setError(caught)
    } finally {
      setPreviewing(false)
    }
  }

  function chosenGroups(): MergeGroupRequest[] {
    return (preview?.groups ?? [])
      .filter((group) => selected.has(group.squashed))
      .map((group) => ({
        field: group.field as RemovalField,
        representative: group.representative,
        spellings: group.variants.map((variant) => variant.value),
      }))
  }

  async function runDiff() {
    const groups = chosenGroups()
    if (groups.length === 0) return
    setError(null)
    setReviewed([])
    setApplied([])
    setReverted([])
    setRewritten(0)
    setJobId(null)
    setBatchId(null)
    setDiffing(true)
    const collected: Target[] = []
    try {
      await api.stream<BatchEvent>(
        '/api/bulk/merge-similar/diff',
        {
          groups,
          selection: {
            kind,
            limit,
            search: search.trim() || null,
            artist_ids: facets.artistIds,
            album_ids: facets.albumIds,
            exclude: facets.patterns,
          },
        },
        (event) => {
          if (event.type === 'item' && 'merge' in event) {
            collected.push({
              item_id: event.item_id,
              name: event.name,
              applicable: event.applicable,
              skipped_reason: event.skipped_reason,
              merge: (event.merge ?? {}) as Record<string, number[]>,
            })
            setReviewed([...collected])
          } else if (event.type === 'summary' && 'merging' in event) {
            setJobId(event.job_id ?? null)
            setBatchId(event.batch_id ?? null)
            setRewritten(event.rewritten_values ?? 0)
          } else if (event.type === 'error') {
            setError(new Error(event.message))
          }
        },
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setDiffing(false)
    }
  }

  async function runApply() {
    if (!jobId) return
    setError(null)
    setApplied([])
    setApplying(true)
    try {
      await api.stream<BatchEvent>(
        '/api/bulk/merge-similar/apply',
        { job_id: jobId, confirm: true },
        (event) => {
          if (event.type === 'summary' && 'merging' in event && event.batch_id) {
            setBatchId(event.batch_id)
          }
          if (event.type !== 'item' && event.type !== 'done') {
            setApplied((current) => [...current, event as BulkEvent])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setApplying(false)
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  async function runRevert() {
    if (!batchId) return
    setError(null)
    setReverted([])
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
      )
    } catch (caught) {
      setError(caught)
    } finally {
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  const groups = preview?.groups ?? []
  const applicable = (reviewed ?? []).filter((item) => item.applicable).length

  return (
    <section>
      <h2>Merge similar genres</h2>
      <p className="muted small">
        Finds values that are the same genre spelled differently — <span className="mono">Hip Hop</span>,{' '}
        <span className="mono">hip-hop</span> and <span className="mono">HipHop</span> — and proposes one
        spelling for each group. Nothing is merged automatically: this preview writes nothing, and only
        the groups you confirm become a reviewed, revertible batch.
      </p>

      <div className="panel">
        <div className="row">
          <div className="pill-row">
            {KINDS.map((entry) => (
              <button
                key={entry.value}
                className={entry.value === kind ? 'pill active' : 'pill'}
                onClick={() => setKind(entry.value)}
              >
                {entry.label}
              </button>
            ))}
          </div>
          {FIELDS.map((field) => (
            <label className="check" key={field}>
              <input
                type="checkbox"
                checked={fields.includes(field)}
                onChange={() =>
                  setFields(
                    fields.includes(field)
                      ? fields.filter((entry) => entry !== field)
                      : [...fields, field],
                  )
                }
              />
              {field}
            </label>
          ))}
        </div>

        <div className="row" style={{ alignItems: 'flex-start' }}>
          <label className="check" htmlFor="merge-keep">
            never touch (whole group)
          </label>
          <textarea
            id="merge-keep"
            rows={2}
            value={keep}
            placeholder="one per line, e.g. Goth"
            onChange={(event) => setKeep(event.target.value)}
            style={{ minWidth: '14rem' }}
          />
          <label className="check" htmlFor="merge-exceptions">
            keep this spelling
          </label>
          <textarea
            id="merge-exceptions"
            rows={2}
            value={exceptions}
            placeholder="one per line, e.g. hip-hop"
            onChange={(event) => setExceptions(event.target.value)}
            style={{ minWidth: '14rem' }}
          />
        </div>

        <div className="row">
          <button className="primary" onClick={runPreview} disabled={previewing || fields.length === 0}>
            {previewing ? 'Looking…' : 'Find similar genres'}
          </button>
          <span className="muted small">Read-only: this step writes nothing.</span>
        </div>
      </div>

      <ErrorNote error={error} />

      {preview && (
        <div className="panel">
          <h3>
            Groups found{' '}
            <span className="muted small">
              {groups.length} group(s) · {preview.distinct_values} distinct value(s) in {preview.source}
            </span>
          </h3>

          {preview.truncated && (
            <p className="badge warn">
              The scan stopped at its cap, so this covers part of the library and the list below is a
              lower bound.
            </p>
          )}

          {(preview.hints ?? []).length > 0 && (
            <div>
              <p className="muted small">Worth knowing:</p>
              <ul className="muted small">
                {(preview.hints ?? []).map((hint) => (
                  <li key={hint}>{hint}</li>
                ))}
              </ul>
            </div>
          )}

          {groups.length === 0 ? (
            <p className="badge warn">
              No spelling variants found. Every value in this vocabulary is already distinct — similarity
              compares case and separators only, so <span className="mono">Rock</span> and{' '}
              <span className="mono">Rockabilly</span> are deliberately not a group.
            </p>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Use</th>
                  <th>Field</th>
                  <th>Keep</th>
                  <th>Rewrite</th>
                  <th>Occurrences</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group: SimilarityGroupView) => (
                  <tr key={`${group.field}-${group.squashed}`}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`merge ${group.representative}`}
                        checked={selected.has(group.squashed)}
                        onChange={() => {
                          const next = new Set(selected)
                          if (next.has(group.squashed)) next.delete(group.squashed)
                          else next.add(group.squashed)
                          setSelected(next)
                        }}
                      />
                    </td>
                    <td className="mono">{group.field}</td>
                    <td>
                      <span className="chip">{group.representative}</span>
                    </td>
                    <td>
                      <span className="chips">
                        {Object.keys(group.merge).map((from) => (
                          <span className="chip danger-chip" key={from}>
                            {from}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td>{group.total}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {groups.length > 0 && (
            <div className="row">
              <button className="primary" onClick={runDiff} disabled={diffing || selected.size === 0}>
                {diffing ? 'Reviewing…' : `Review ${selected.size} group(s)`}
              </button>
              <span className="muted small">
                Sets the selection first; nothing is written until you apply the review.
              </span>
            </div>
          )}
        </div>
      )}

      <div className="panel">
        <SelectionFilters
          kind={kind}
          values={facets}
          onChange={setFacets}
          idPrefix="merge"
        />
        <div className="row">
          <label className="check">
            search
            <input
              value={search}
              placeholder="optional"
              onChange={(event) => setSearch(event.target.value)}
              style={{ width: '9rem' }}
            />
          </label>
          <label className="check">
            limit
            <input
              type="number"
              min={1}
              max={500}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              style={{ width: '5rem' }}
            />
          </label>
        </div>
      </div>

      {reviewed && (
        <div className="panel">
          <h3>
            Reviewed{' '}
            <span className="muted small">
              {reviewed.length} matched · {applicable} would change · {rewritten} value(s) rewritten
            </span>
          </h3>
          {reviewed.length === 0 ? (
            <p className="badge warn">No item in this selection carries a value from the chosen groups.</p>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Field</th>
                  <th>Rewriting</th>
                </tr>
              </thead>
              <tbody>
                {reviewed.map((item) => {
                  const entries = Object.entries(item.merge)
                  if (entries.length === 0) {
                    return (
                      <tr key={item.item_id}>
                        <td>
                          <Link to={`/edit/${item.item_id}`}>{item.name}</Link>
                        </td>
                        <td colSpan={2} className="muted">
                          {item.skipped_reason ?? 'nothing to change'}
                        </td>
                      </tr>
                    )
                  }
                  return entries.map(([field, positions], index) => (
                    <tr key={`${item.item_id}-${field}`}>
                      <td>
                        {index === 0 ? <Link to={`/edit/${item.item_id}`}>{item.name}</Link> : ''}
                      </td>
                      <td className="mono">{field}</td>
                      <td>
                        <span className="muted small">
                          {positions.length} position(s) would be rewritten
                        </span>
                      </td>
                    </tr>
                  ))
                })}
              </tbody>
            </table>
          )}

          {reviewed.length > 0 && (
            <div className="actions">
              <button className="primary" onClick={runApply} disabled={applying || !jobId || applicable === 0}>
                {applying ? 'Applying…' : `Merge into ${applicable} item(s)`}
              </button>
            </div>
          )}
        </div>
      )}

      {applied.length > 0 && (
        <div className="panel">
          <h3>Applied</h3>
          <pre className="log">{JSON.stringify(applied, null, 2)}</pre>
          {batchId && <button onClick={runRevert}>Revert this batch</button>}
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
