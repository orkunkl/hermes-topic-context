/**
 * topic-context — desktop half.
 *
 * A right-hand pane that answers "what is this session about?" while you work:
 * a compiled brief (context / current state / direction), the topics mined from
 * the conversation, the memory entries that match, and past sessions that share
 * the topic.
 *
 * Loaded uncompiled: `jsx()` calls, not JSX syntax. Only `@hermes/plugin-sdk`,
 * `react` and `react/jsx-runtime` resolve. Colors come from theme variables —
 * never hardcoded.
 *
 * All data comes from the plugin's own backend (`ctx.rest('/context')`), which
 * is read-only and makes no LLM calls, so refreshing is free.
 */

import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  Skeleton,
  Streamdown,
  Tip,
  cn,
  haptic,
  host,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'topic-context'
const PANE_TITLE = 'topic context'
const STALE_MS = 5000
/** Safety-net poll. The backend reads local SQLite, so this is cheap; the live
 *  trigger below is what actually keeps the pane current. */
const POLL_MS = 15000

/** Dot colour per mined topic kind, from the app's semantic palette. */
const KIND_COLOR = {
  command: 'var(--ui-blue)',
  file: 'var(--ui-cyan)',
  tool: 'var(--ui-purple)',
  phrase: 'var(--ui-green)',
  ident: 'var(--ui-yellow)',
  term: 'var(--ui-text-quaternary)'
}

/* ------------------------------------------------------------------ helpers */

/** Split markdown emphasis out of a snippet for single-line row display. */
function plainSnippet(snippet) {
  return String(snippet || '').replace(/\*\*/g, '')
}

/** Split `text` into runs, flagging the ones that match any term (case-insensitive). */
function splitMatches(text, terms) {
  const needles = (terms || []).filter(Boolean).map(term => term.toLowerCase())
  if (!needles.length) return [{ text, hit: false }]

  const runs = []
  let buffer = ''
  let bufferHit = null

  const flush = () => {
    if (buffer) runs.push({ text: buffer, hit: Boolean(bufferHit) })
    buffer = ''
    bufferHit = null
  }

  let i = 0
  const lower = text.toLowerCase()
  while (i < text.length) {
    const needle = needles.find(term => lower.startsWith(term, i))
    if (needle) {
      flush()
      runs.push({ text: text.slice(i, i + needle.length), hit: true })
      i += needle.length
    } else {
      if (bufferHit === null) bufferHit = false
      buffer += text[i]
      i += 1
    }
  }
  flush()
  return runs
}

/* --------------------------------------------------------------- primitives */

/** Section title row: chevron (when collapsible), label, and a count badge. */
function SectionLabel({ title, count, collapsible = false, expanded = true }) {
  return jsxs('span', { className: 'flex items-center gap-1.5', children: [
    collapsible
      ? jsx(Codicon, {
          name: expanded ? 'chevron-down' : 'chevron-right',
          className: 'shrink-0 text-(--ui-text-quaternary)'
        })
      : null,
    jsx('span', {
      className: 'text-[0.625rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-quaternary)',
      children: title
    }),
    count === undefined || count === null
      ? null
      : jsx(Badge, { variant: count ? 'muted' : 'outline', size: 'xs', children: String(count) })
  ]})
}

const SECTION_CLASS = 'border-b border-(--ui-stroke-secondary) px-3 py-2.5'

/**
 * A section that folds. Kept separate from `Section` on purpose: the collapse
 * state belongs to the section that actually toggles, so a page of static
 * sections stays hook-free and a test can address this state positionally.
 */
function CollapsibleSection({ t, title, count, action, children, className, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)

  return jsxs('div', { className: cn(SECTION_CLASS, className), children: [
    jsxs('div', { className: 'mb-2 flex items-center gap-1.5', children: [
      jsx('button', {
        type: 'button',
        onClick: () => setOpen(value => !value),
        'aria-expanded': open,
        'aria-label': open ? t('collapse') : t('expand'),
        className: 'flex w-full cursor-pointer items-center gap-1.5 text-left',
        children: jsx(SectionLabel, { title, count, collapsible: true, expanded: open })
      }),
      action ? jsx('div', { className: 'ml-auto flex items-center gap-1', children: action }) : null
    ]}),
    open ? jsx('div', { children }) : null
  ]})
}

function Section({ t, title, count, action, children, className, collapsible = false, defaultOpen = true }) {
  if (collapsible) {
    return jsx(CollapsibleSection, { t, title, count, action, className, defaultOpen, children })
  }
  return jsxs('div', { className: cn(SECTION_CLASS, className), children: [
    jsxs('div', { className: 'mb-2 flex items-center gap-1.5', children: [
      jsx(SectionLabel, { title, count }),
      action ? jsx('div', { className: 'ml-auto flex items-center gap-1', children: action }) : null
    ]}),
    jsx('div', { children })
  ]})
}

function IconButton({ icon, label, onClick, active }) {
  return jsx(Tip, {
    label,
    children: jsx(Button, {
      variant: 'ghost',
      size: 'icon-xs',
      type: 'button',
      onClick,
      className: cn(active && 'bg-(--chrome-action-hover) text-(--ui-text-primary)'),
      'aria-label': label,
      children: jsx(Codicon, { name: icon })
    })
  })
}

/* ------------------------------------------------------------------- pieces */

function Brief({ t, brief, source, raw, onToggleRaw }) {
  return jsx('div', {
    className: 'overflow-hidden rounded-[4px] border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary)',
    children: jsxs('div', { children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5 border-b border-(--ui-stroke-secondary) px-2 py-1.5',
        children: [
          jsx(Codicon, { name: 'note', className: 'text-(--ui-text-quaternary)' }),
          jsx('span', {
            className: 'text-[0.625rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-quaternary)',
            children: 'brief.md'
          }),
          jsx('span', { className: 'ml-auto text-[0.625rem] text-(--ui-text-quaternary)', children: source }),
          jsx(IconButton, {
            icon: raw ? 'eye' : 'code',
            label: raw ? t('showRendered') : t('showSource'),
            onClick: onToggleRaw,
            active: raw
          })
        ]
      }),
      raw
        ? jsx('pre', {
            className: 'overflow-x-auto px-2 py-2 font-mono text-[0.6875rem] leading-[1.6] whitespace-pre-wrap text-(--ui-text-secondary)',
            children: brief
          })
        : jsx('div', {
            className: 'px-2 py-1 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)',
            children: jsx(Streamdown, {
              mode: 'static',
              controls: false,
              parseIncompleteMarkdown: false,
              children: brief
            })
          })
    ]})
  })
}

function TopicChips({ t, topics, active, onPick }) {
  if (!topics.length) {
    return jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('noTopics') })
  }
  const chip = (label, kind, isActive, onClick, dotColor) =>
    jsxs('button', {
      type: 'button',
      onClick,
      className: cn(
        'inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-[0.6875rem] transition-colors',
        isActive
          ? 'border-(--ui-accent) bg-(--ui-bg-secondary) text-(--ui-text-primary)'
          : 'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
      ),
      children: [
        jsx('span', {
          className: 'size-1.5 shrink-0 rounded-full',
          style: { background: dotColor }
        }),
        jsx('span', { className: 'truncate', children: label })
      ]
    })

  return jsxs('div', { className: 'flex flex-wrap gap-1', children: [
    chip(t('all'), 'all', active === null, () => onPick(null), 'var(--ui-text-quaternary)'),
    ...topics.map(topic =>
      chip(
        topic.label,
        topic.kind,
        active === topic.label,
        () => onPick(active === topic.label ? null : topic.label),
        KIND_COLOR[topic.kind] || KIND_COLOR.term
      )
    )
  ]})
}

function MemoryList({ t, entries }) {
  if (!entries.length) {
    return jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('noMemory') })
  }
  return jsx('div', { className: 'flex flex-col gap-1.5', children: entries.map((entry, index) =>
    jsxs('div', {
      className: 'rounded-[3px] border border-(--ui-stroke-secondary) bg-(--ui-bg-quaternary) px-2 py-1.5',
      children: [
        jsxs('div', { className: 'mb-0.5 flex items-center gap-1.5', children: [
          jsx('span', { className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: entry.source }),
          entry.matches && entry.matches.length
            ? jsx('span', {
                className: 'ml-auto truncate text-[0.625rem] text-(--ui-text-quaternary)',
                children: entry.matches.join(', ')
              })
            : null
        ]}),
        jsx('p', { className: 'text-[0.6875rem] leading-snug text-(--ui-text-secondary)', children:
          splitMatches(entry.text, entry.matches).map((run, runIndex) =>
            run.hit
              ? jsx('span', {
                  key: runIndex,
                  className: 'rounded-[2px] bg-(--ui-bg-secondary) text-(--ui-text-primary)',
                  children: run.text
                })
              : run.text
          )
        })
      ]
    }, index)
  )})
}

function SessionList({ t, sessions }) {
  if (!sessions.length) {
    return jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: t('noSessions') })
  }
  return jsx('div', { className: 'flex flex-col gap-0.5', children: sessions.map((item, index) =>
    jsxs('button', {
      type: 'button',
      onClick: () => {
        haptic('tap')
        host.openSession(item.session_id)
      },
      className: cn(
        'flex w-full flex-col gap-0.5 rounded-[3px] px-1.5 py-1 text-left transition-colors',
        'hover:bg-(--chrome-action-hover)'
      ),
      children: [
        jsxs('span', { className: 'flex w-full items-baseline gap-1.5', children: [
          jsx('span', { className: 'truncate text-[0.75rem] text-(--ui-text-secondary)', children: item.title }),
          jsx('span', { className: 'ml-auto shrink-0 font-mono text-[0.625rem] text-(--ui-text-quaternary)', children: item.day })
        ]}),
        item.snippet
          ? jsx('span', {
              className: 'line-clamp-2 text-[0.6875rem] leading-snug text-(--ui-text-quaternary)',
              children: plainSnippet(item.snippet)
            })
          : null
      ]
    }, index)
  )})
}

/* --------------------------------------------------------------------- pane */

function Pane({ ctx }) {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const storedId = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)

  const [filter, setFilter] = useState(null)
  const [raw, setRaw] = useState(false)

  const query = useQuery({
    queryKey: [ID, ctx.source, storedId],
    enabled: Boolean(storedId),
    staleTime: STALE_MS,
    refetchInterval: POLL_MS,
    queryFn: () => ctx.rest(`/context?session_id=${encodeURIComponent(storedId)}`)
  })

  // The live trigger: one refresh when the focused chat stops working, so the
  // brief reflects the turn that just finished instead of waiting out the poll.
  useEffect(() => {
    if (!busy && storedId) {
      queryClient.invalidateQueries({ queryKey: [ID, ctx.source, storedId] })
    }
  }, [busy, storedId, ctx.source, queryClient])

  // A new session invalidates any chip the old one had selected.
  useEffect(() => {
    setFilter(null)
  }, [storedId])

  if (!storedId) {
    return jsx('div', {
      className: 'flex h-full items-center justify-center p-4',
      children: jsx(EmptyState, { title: t('noSession'), description: t('noSessionHint') })
    })
  }

  if (query.isPending) {
    return jsxs('div', { className: 'flex flex-col gap-2 p-3', children: [
      jsx(Skeleton, { className: 'h-4 w-24' }),
      jsx(Skeleton, { className: 'h-20 w-full' }),
      jsx(Skeleton, { className: 'h-4 w-32' }),
      jsx(Skeleton, { className: 'h-10 w-full' })
    ]})
  }

  if (query.isError) {
    return jsx('div', {
      className: 'flex h-full items-center justify-center p-4',
      children: jsx(ErrorState, {
        title: t('errorTitle'),
        description: t('errorHint'),
        children: jsx(Button, {
          variant: 'outline',
          size: 'sm',
          type: 'button',
          onClick: () => query.refetch(),
          children: t('retry')
        })
      })
    })
  }

  const data = query.data || {}
  const topics = data.topics || []
  const sessions = filter
    ? (data.sessions || []).filter(item => plainSnippet(item.snippet).toLowerCase().includes(filter.toLowerCase()))
    : (data.sessions || [])
  const memory = filter
    ? (data.memory || []).filter(entry => (entry.matches || []).includes(filter))
    : (data.memory || [])

  const header = jsxs('div', {
    className: 'flex shrink-0 items-center gap-1.5 border-b border-(--ui-stroke-secondary) px-2.5 py-2',
    children: [
      jsx(Codicon, { name: 'compass', className: 'text-(--ui-accent)' }),
      jsx('span', { className: 'text-[0.75rem] font-semibold', children: t('paneTitle') }),
      jsx(Tip, {
        label: t('watching'),
        children: jsx('span', { className: 'size-1.5 rounded-full bg-(--ui-green)' })
      }),
      jsx('div', { className: 'ml-auto flex items-center gap-1', children: [
        jsx(IconButton, {
          icon: 'refresh',
          label: t('refresh'),
          onClick: () => query.refetch(),
          active: query.isFetching
        })
      ]})
    ]
  })

  return jsxs('div', { className: 'flex h-full min-h-0 flex-col', children: [
    header,
    jsxs('div', { className: 'min-h-0 flex-1 overflow-y-auto', children: [
      jsx(Section, {
        t,
        title: t('sectionBrief'),
        children: jsx(Brief, {
          t,
          brief: data.brief || '',
          source: data.brief_source === 'authored' ? t('authored') : t('compiled'),
          raw,
          onToggleRaw: () => setRaw(value => !value)
        })
      }),
      jsx(Section, {
        t,
        title: t('sectionTopics'),
        count: topics.length,
        children: jsx(TopicChips, { t, topics, active: filter, onPick: setFilter })
      }),
      // Collapsed by default: this section is often empty, and a one-line header
      // with its match count is a better empty state than a paragraph saying so.
      jsx(Section, {
        t,
        title: t('sectionMemory'),
        count: memory.length,
        collapsible: true,
        defaultOpen: false,
        children: jsx(MemoryList, { t, entries: memory })
      }),
      jsx(Section, {
        t,
        title: t('sectionSessions'),
        count: sessions.length,
        className: 'border-b-0',
        children: jsx(SessionList, { t, sessions })
      }),
      data.cognee && data.cognee.enabled === false
        ? jsx('div', {
            className: 'px-3 pb-3 text-[0.625rem] text-(--ui-text-quaternary)',
            children: t('cogneeOff')
          })
        : null
    ]})
  ]})
}

/* ----------------------------------------------------------------- register */

export default {
  id: ID,
  name: 'Topic Context',

  register(ctx) {
    ctx.i18n.register({
      en: {
        paneTitle: 'Topic Context',
        noSession: 'No session in focus',
        noSessionHint: 'Open a chat and this pane will describe what it is about.',
        errorTitle: 'Could not read the session store',
        errorHint: 'The plugin backend is not answering. Is “topic-context” in plugins.enabled?',
        retry: 'Retry',
        watching: 'Following the focused session',
        refresh: 'Refresh',
        sectionBrief: 'Brief',
        sectionTopics: 'Topics',
        sectionMemory: 'Memory',
        sectionSessions: 'Related sessions',
        compiled: 'compiled',
        authored: 'authored',
        showSource: 'Show markdown source',
        showRendered: 'Show rendered markdown',
        expand: 'Expand section',
        collapse: 'Collapse section',
        all: 'All',
        noTopics: 'No distinctive topics yet.',
        noMemory: 'No memory entry matches this topic.',
        noSessions: 'No earlier session shares this topic.',
        cogneeOff: 'Cognee graph layer off — enable the provider to include it.'
      },
      tr: {
        paneTitle: 'Bağlam',
        noSession: 'Odakta session yok',
        noSessionHint: 'Bir sohbet aç; bu panel ne hakkında olduğunu anlatır.',
        errorTitle: 'Session deposu okunamadı',
        errorHint: 'Plugin backend yanıt vermiyor. “topic-context” plugins.enabled içinde mi?',
        retry: 'Yeniden dene',
        watching: 'Odaktaki session izleniyor',
        refresh: 'Yenile',
        sectionBrief: 'Brief',
        sectionTopics: 'Konular',
        sectionMemory: 'Hafıza',
        sectionSessions: 'İlgili sessionlar',
        compiled: 'derlendi',
        authored: 'yazıldı',
        showSource: 'Markdown kaynağını göster',
        showRendered: 'Render edilmiş markdown',
        expand: 'Bölümü aç',
        collapse: 'Bölümü kapat',
        all: 'Tümü',
        noTopics: 'Henüz belirgin bir konu yok.',
        noMemory: 'Bu konuyla eşleşen hafıza kaydı yok.',
        noSessions: 'Bu konuyu paylaşan geçmiş session yok.',
        cogneeOff: 'Cognee graph katmanı kapalı — eklemek için provider\'ı etkinleştir.'
      }
    })

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: PANE_TITLE,
      data: { placement: 'right', width: '330px' },
      render: () => jsx(Pane, { ctx })
    })
  }
}
