/**
 * Desktop-half smoke test.
 *
 * The pane can only be *seen* in the desktop app, but almost everything that
 * breaks a disk plugin surfaces here: an import that does not resolve, an
 * identifier used in a jsx() call but never imported, a wrong contribution
 * area, a locale bundle missing a key, or a branch that throws.
 *
 * It loads the real `desktop/plugin.js` in a VM with synthetic stand-ins for
 * the three specifiers the host resolves (and for the React hooks), then
 * *executes* every component in the rendered tree rather than only calling
 * render() — so a component body that reaches for an unimported name fails
 * here instead of painting a blank pane.
 *
 * Run: node --experimental-vm-modules tests/desktop_smoke.mjs
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_JS = resolve(HERE, '../desktop/plugin.js')

/* --------------------------------------------------------- synthetic SDK */

const SDK_SOURCE = `
const ATOM = name => ({ __atom: name })
const CALLS = () => globalThis.__topicContextCalls
export const host = {
  state: { focusedStoredSessionId: ATOM('focusedStoredSessionId'), busy: ATOM('busy') },
  openSession: id => { CALLS().opened.push(id) }
}
export const haptic = () => { CALLS().haptics++ }
export const useValue = atom => (globalThis.__hooks.atoms || {})[atom.__atom]
export const useQuery = options => { CALLS().queries.push(options); return globalThis.__hooks.query }
export const useQueryClient = () => ({ invalidateQueries: () => { CALLS().invalidated++ } })
export const usePluginI18n = () => (key, arg) => {
  CALLS().i18nKeys.add(key)
  return arg === undefined ? key : key + ':' + String(arg)
}
export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const Tip = 'Tip'
export const Badge = 'Badge'
export const Button = 'Button'
export const Codicon = 'Codicon'
export const EmptyState = 'EmptyState'
export const ErrorState = 'ErrorState'
export const Skeleton = 'Skeleton'
export const Streamdown = 'Streamdown'
`

const REACT_SOURCE = `
export const useState = initial => {
  const scripted = globalThis.__hooks.values.shift()
  return [scripted === undefined ? initial : scripted, () => {}]
}
export const useEffect = fn => { globalThis.__topicContextCalls.effects.push(fn) }
`

const JSX_SOURCE = `
export const jsx = (type, props, key) => ({ type, props: props || {}, key })
export const jsxs = (type, props, key) => ({ type, props: props || {}, key })
`

/* -------------------------------------------------------------- the loader */

async function loadPlugin() {
  const sandbox = {
    console,
    __hooks: null,
    __topicContextCalls: { opened: [], haptics: 0, queries: [], effects: [], invalidated: 0, i18nKeys: new Set() }
  }
  vm.createContext(sandbox)

  const stubs = new Map()
  for (const [specifier, text] of Object.entries({
    '@hermes/plugin-sdk': SDK_SOURCE,
    react: REACT_SOURCE,
    'react/jsx-runtime': JSX_SOURCE
  })) {
    const stub = new vm.SourceTextModule(text, { context: sandbox, identifier: specifier })
    await stub.link(() => {
      throw new Error(`stub ${specifier} must not import`)
    })
    await stub.evaluate()
    stubs.set(specifier, stub)
  }

  const source = readFileSync(PLUGIN_JS, 'utf8')
  const module = new vm.SourceTextModule(source, { context: sandbox, identifier: PLUGIN_JS })
  await module.link(specifier => {
    const stub = stubs.get(specifier)
    if (!stub) throw new Error(`the host cannot resolve this import: ${specifier}`)
    return stub
  })
  await module.evaluate()

  return { plugin: module.namespace.default, sandbox, calls: sandbox.__topicContextCalls }
}

/* ------------------------------------------------------------- rendering */

/** The values the hooks return for one render pass. */
function hookValues({ storedId = 's1', busy = false, query = {}, states = [] } = {}) {
  return {
    atoms: { focusedStoredSessionId: storedId, busy },
    query,
    values: [...states]
  }
}

/**
 * Execute a tree: invoke every function component (with a fresh hook read
 * cursor, exactly as React would for a first render) and flatten the result
 * into the list of host elements the pane actually produces.
 */
function expand(node, sandbox, values, out = []) {
  if (node === null || node === undefined || typeof node !== 'object') return out
  if (Array.isArray(node)) {
    for (const child of node) expand(child, sandbox, values, out)
    return out
  }
  const { type, props } = node
  if (typeof type === 'function') {
    out.push({ __component: type.name || '(anonymous component)', props: props || {} })
    sandbox.__hooks = values
    expand(type(props || {}), sandbox, values, out)
    return out
  }
  out.push(node)
  if (props && 'children' in props) expand(props.children, sandbox, values, out)
  return out
}

function harness() {
  const registrations = []
  const localeBundles = []
  const ctx = {
    source: 'test',
    register: contribution => registrations.push(contribution),
    registerMany: list => list.forEach(item => registrations.push(item)),
    i18n: { register: bundles => localeBundles.push(bundles) },
    rest: async () => ({}),
    storage: { get: () => undefined, set: () => {}, remove: () => {} }
  }
  return { ctx, registrations, localeBundles }
}

/* -------------------------------------------------------------------- data */

const PAYLOAD = {
  session: { id: 's1', title: 'Wire the pane', source: 'desktop', day: '2026-10-17' },
  brief: '## Context\n\nA session about plugin.js.\n\n## Current state\n\n- Working\n\n## Direction\n\n- `[ ]` Ship it',
  brief_source: 'compiled',
  topics: [
    { label: 'plugin.js', kind: 'file', count: 3, score: 9 },
    { label: '/plan', kind: 'command', count: 1, score: 4.8 },
    { label: 'write_file', kind: 'tool', count: 1, score: 1.6 }
  ],
  memory: [{ source: 'MEMORY.md', text: 'plugin.js lives under desktop-plugins', matches: ['plugin.js'] }],
  sessions: [{ session_id: 's9', title: 'Prior work', day: '2026-10-16', snippet: 'a **plugin.js** mention' }],
  cognee: { enabled: false }
}

const SETTLED = { isPending: false, isError: false, isFetching: false, data: PAYLOAD, refetch: () => {} }

/* -------------------------------------------------------------------- main */

const results = []
function check(name, fn) {
  try {
    fn()
    results.push({ ok: true, name })
  } catch (error) {
    results.push({ ok: false, name, error })
    process.exitCode = 1
  }
}

const { plugin, sandbox, calls } = await loadPlugin()

check('default export declares the plugin id and a register()', () => {
  assert.equal(plugin.id, 'topic-context')
  assert.equal(typeof plugin.name, 'string')
  assert.equal(typeof plugin.register, 'function')
})

const { ctx, registrations, localeBundles } = harness()
plugin.register(ctx)

check('registers exactly one contribution', () => {
  assert.equal(registrations.length, 1)
})

const pane = registrations[0]

check('contributes a right-docked pane with an explicit width', () => {
  assert.equal(pane.area, 'panes')
  assert.equal(pane.data.placement, 'right')
  assert.equal(pane.data.width, '330px')
  assert.equal(typeof pane.render, 'function')
})

check('ships en + tr locale bundles covering the same keys', () => {
  assert.equal(localeBundles.length, 1)
  const [bundles] = localeBundles
  assert.ok(bundles.en, 'missing en bundle')
  assert.ok(bundles.tr, 'missing tr bundle')
  assert.deepEqual(Object.keys(bundles.en).sort(), Object.keys(bundles.tr).sort())
  for (const [key, value] of Object.entries(bundles.en)) {
    assert.ok(String(value).length, `empty en.${key}`)
  }
})

/* --- branch coverage: every state the pane can be in */

const branches = [
  ['no session in focus', hookValues({ storedId: null, query: { isPending: false, isError: false } })],
  ['loading', hookValues({ storedId: 's1', query: { isPending: true, isError: false } })],
  ['error', hookValues({ storedId: 's1', query: { isPending: false, isError: true, refetch: () => {} } })],
  ['data', hookValues({ storedId: 's1', query: SETTLED })],
  ['data, markdown source view', hookValues({ storedId: 's1', query: SETTLED, states: [null, true] })],
  ['empty payload', hookValues({
    storedId: 's1',
    query: { ...SETTLED, data: { ...PAYLOAD, topics: [], memory: [], sessions: [] } }
  })],
  ['payload with no cognee layer', hookValues({
    storedId: 's1',
    query: { ...SETTLED, data: { ...PAYLOAD, cognee: { enabled: true } } }
  })]
]

for (const [label, values] of branches) {
  check(`renders without throwing: ${label}`, () => {
    const fresh = harness()
    plugin.register(fresh.ctx)
    const nodes = expand(fresh.registrations[0].render(), sandbox, values)
    assert.ok(nodes.length > 0, 'the pane produced no elements')
  })
}

check('every label the pane renders exists in both locale bundles', () => {
  const [bundles] = localeBundles
  calls.i18nKeys.clear()
  const fresh = harness()
  plugin.register(fresh.ctx)
  expand(fresh.registrations[0].render(), sandbox, hookValues({ query: SETTLED }))
  assert.ok(calls.i18nKeys.size > 5, 'no labels were requested — the stub is not wired')
  for (const key of calls.i18nKeys) {
    assert.ok(key in bundles.en, `en is missing ${key}`)
    assert.ok(key in bundles.tr, `tr is missing ${key}`)
  }
})

check('a related session opens through host.openSession', () => {
  const fresh = harness()
  plugin.register(fresh.ctx)
  const nodes = expand(fresh.registrations[0].render(), sandbox, hookValues({ query: SETTLED }))
  calls.opened.length = 0
  const row = nodes.find(node =>
    node.type === 'button' &&
    typeof node.props.onClick === 'function' &&
    String(node.props.className).includes('flex-col')
  )
  assert.ok(row, 'no clickable session row was rendered')
  row.props.onClick()
  assert.deepEqual([...calls.opened], ['s9'])
})

check('the query is keyed on the focused session and enabled by it', () => {
  const fresh = harness()
  plugin.register(fresh.ctx)
  calls.queries.length = 0
  expand(fresh.registrations[0].render(), sandbox, hookValues({ storedId: 'abc', query: SETTLED }))
  assert.equal(calls.queries.length, 1)
  const [options] = calls.queries
  assert.equal(options.queryKey.at(-1), 'abc')
  assert.equal(options.enabled, true)
  assert.equal(typeof options.queryFn, 'function')
})

check('a topic chip filters the lists without issuing a new request', () => {
  const fresh = harness()
  plugin.register(fresh.ctx)
  calls.queries.length = 0
  const nodes = expand(fresh.registrations[0].render(), sandbox, hookValues({ query: SETTLED }))
  // Chips are the pill-shaped buttons; the session rows are the flex-col ones.
  const chips = nodes.filter(node =>
    node.type === 'button' && typeof node.props.onClick === 'function' &&
    String(node.props.className).includes('rounded-full')
  )
  assert.ok(chips.length >= 4, `expected an 'all' chip plus one per topic, found ${chips.length}`)
  chips[1].props.onClick()
  assert.equal(calls.queries.length, 1, 'filtering must not refetch — it is local state')
})

check('a session with no topics and no matches still renders its sections', () => {
  const fresh = harness()
  plugin.register(fresh.ctx)
  const nodes = expand(fresh.registrations[0].render(), sandbox, hookValues({
    query: { ...SETTLED, data: { ...PAYLOAD, topics: [], memory: [], sessions: [] } }
  }))
  const text = JSON.stringify(nodes.map(node => node.props && node.props.children))
  assert.ok(text.includes('noTopics'), 'the empty topic state is missing')
  assert.ok(text.includes('noMemory'), 'the empty memory state is missing')
  assert.ok(text.includes('noSessions'), 'the empty session state is missing')
})

/* ------------------------------------------------------------------ report */

console.log(`desktop half — ${PLUGIN_JS}`)
for (const { ok, name, error } of results) {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`)
  if (!ok) console.log(`       ${error.message.split('\n')[0]}`)
}
const failed = results.filter(result => !result.ok).length
console.log(`\n${results.length - failed} passed, ${failed} failed`)
