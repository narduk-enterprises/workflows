// Publishes this repository's design mirrors to the designs Worker
// (operator-portal#1613, #727 slice 3). It only READS `design/**`.
//
//   node script/ci/publish_designs.mjs [--dry-run] [--design-dir <dir>]
//
// For each design folder under `design/` that holds a `canvas.json` (at its
// root, or under its `project/` folder, as a Claude Design export lays it out)
// it uploads every file to `PUT /_publish/<repo>/<slug>/<sha>/<file>` on the
// designs Worker, then `POST .../_finalize` with the file list. The Worker
// checks the stored set against that list and only then moves the `latest`
// pointer, so a half-uploaded mirror is never what the portal reads.
//
// Authentication is the run's own GitHub Actions OIDC token, audience the
// Worker's origin: there is no stored Cloudflare or R2 credential. The Worker
// admits a token for a repository of this organization on `main`, writing
// under that repository's own name.
//
// Environment: DESIGNS_ORIGIN, GITHUB_SHA, GITHUB_REPOSITORY, and the two
// ACTIONS_ID_TOKEN_REQUEST_* variables a job with `id-token: write` receives.

import { readdir, readFile, stat } from 'node:fs/promises'
import path from 'node:path'

const SLUG = /^[a-z0-9][a-z0-9._-]{0,99}$/
const SHA = /^[0-9a-f]{40}$/
const MAX_FILE_BYTES = 25 * 1024 * 1024
const MAX_FILES = 800
const CONCURRENCY = 6

/** Folders under `designDir` that are designs, each with the folder its files are read from. */
export async function designRoots(designDir) {
  const roots = []
  for (const entry of (await readdir(designDir, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory() || entry.name === 'archive' || !SLUG.test(entry.name)) continue
    for (const candidate of [path.join(designDir, entry.name), path.join(designDir, entry.name, 'project')]) {
      if (await stat(path.join(candidate, 'canvas.json')).then((s) => s.isFile(), () => false)) {
        roots.push({ root: candidate, slug: entry.name })
        break
      }
    }
  }
  return roots
}

/** Every publishable file under `root`, as `{ file, size }` with `/` separators, sorted. */
export async function listFiles(root, prefix = '') {
  const files = []
  for (const entry of await readdir(path.join(root, prefix), { withFileTypes: true })) {
    if (entry.name.startsWith('.') || entry.name === 'node_modules') continue
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) files.push(...(await listFiles(root, relative)))
    else if (entry.isFile()) files.push({ file: relative, size: (await stat(path.join(root, relative))).size })
  }
  // Code-unit order, so the list is the same on every runner and locale.
  return files.sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0))
}

const encodePath = (file) => file.split('/').map(encodeURIComponent).join('/')

async function requestToken(env) {
  const response = await fetch(`${env.ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${encodeURIComponent(env.DESIGNS_ORIGIN)}`, {
    headers: { authorization: `Bearer ${env.ACTIONS_ID_TOKEN_REQUEST_TOKEN}` },
    signal: AbortSignal.timeout(15_000),
  })
  if (!response.ok) throw new Error(`the OIDC token request answered ${response.status}`)
  const { value } = await response.json()
  if (typeof value !== 'string' || !value) throw new Error('the OIDC token request returned no token')
  return value
}

/** One call to the Worker, retried on a 5xx or a network failure; anything else is final. */
async function call(url, init, token) {
  let last = ''
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await fetch(url, { ...init, headers: { ...init.headers, authorization: `Bearer ${await token()}` }, signal: AbortSignal.timeout(60_000) })
      if (response.status < 500) return response
      last = `answered ${response.status}`
    } catch (error) {
      last = error instanceof Error ? error.message : String(error)
    }
    await new Promise((resolve) => setTimeout(resolve, attempt * 1000))
  }
  throw new Error(`${init.method} ${url} ${last}`)
}

async function publishOne({ origin, repo, root, sha, slug }, token) {
  const files = await listFiles(root)
  if (files.length === 0 || files.length > MAX_FILES) throw new Error(`${slug}: ${files.length} files; the Worker takes 1 to ${MAX_FILES}`)
  const oversize = files.find((f) => f.size > MAX_FILE_BYTES)
  if (oversize) throw new Error(`${slug}: ${oversize.file} is ${oversize.size} bytes; the limit is ${MAX_FILE_BYTES}`)
  const base = `${origin}/_publish/${encodeURIComponent(repo)}/${encodeURIComponent(slug)}/${sha}`

  const queue = [...files]
  const failures = []
  await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
    for (let item = queue.shift(); item; item = queue.shift()) {
      const body = await readFile(path.join(root, item.file))
      const response = await call(`${base}/${encodePath(item.file)}`, { method: 'PUT', body, headers: { 'content-type': 'application/octet-stream' } }, token)
      if (!response.ok) failures.push(`${item.file}: ${response.status} ${await response.text()}`)
    }
  }))
  if (failures.length > 0) throw new Error(`${slug}: ${failures.length} uploads failed, first: ${failures[0]}`)

  const finalized = await call(`${base}/_finalize`, {
    method: 'POST',
    body: JSON.stringify({ files: files.map((f) => ({ path: f.file, size: f.size })) }),
    headers: { 'content-type': 'application/json' },
  }, token)
  if (!finalized.ok) throw new Error(`${slug}: finalize answered ${finalized.status} ${await finalized.text()}`)
  return files.length
}

/** Publishes every design folder under `designDir`; returns `[{ slug, files }]`. Throws on the first failure. */
export async function publishAll({ designDir, env }) {
  const origin = (env.DESIGNS_ORIGIN ?? '').replace(/\/$/, '')
  const sha = env.GITHUB_SHA ?? ''
  const repo = (env.GITHUB_REPOSITORY ?? '').split('/')[1] ?? ''
  if (!/^https:\/\/[^/]+$/.test(origin)) throw new Error('DESIGNS_ORIGIN must be an https origin')
  if (!SHA.test(sha) || !repo) throw new Error('GITHUB_SHA and GITHUB_REPOSITORY are required')
  if (!env.ACTIONS_ID_TOKEN_REQUEST_URL || !env.ACTIONS_ID_TOKEN_REQUEST_TOKEN) throw new Error('no OIDC token request: the job needs `permissions: id-token: write`')
  const roots = await designRoots(designDir)
  if (roots.length === 0) throw new Error('no design folder holds a canvas.json')

  let cached = null
  const token = async () => {
    if (!cached || Date.now() - cached.at > 3 * 60_000) cached = { at: Date.now(), value: await requestToken(env) }
    return cached.value
  }
  const published = []
  for (const { root, slug } of roots) published.push({ slug, files: await publishOne({ origin, repo, root, sha, slug }, token) })
  return published
}

async function main() {
  const args = process.argv.slice(2)
  const designDir = args.includes('--design-dir') ? args[args.indexOf('--design-dir') + 1] : 'design'
  if (args.includes('--dry-run')) {
    for (const { root, slug } of await designRoots(designDir)) console.log(`${slug}: ${(await listFiles(root)).length} files from ${root}`)
    return
  }
  for (const { files, slug } of await publishAll({ designDir, env: process.env })) console.log(`${slug}: published ${files} files at ${process.env.GITHUB_SHA}`)
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(`::error title=Publish designs::${error instanceof Error ? error.message : String(error)}`)
    process.exit(1)
  })
}
