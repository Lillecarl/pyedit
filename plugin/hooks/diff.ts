/**
 * Reads what pyedit's MCP `run` tool returns, and cuts its diff into one
 * section per file for the `Code` element, which draws only whole hunks.
 */

/** The fields of pyedit's result that the box shows. */
export type RunResult = {
  ok: boolean
  error?: string
  diff?: string
  diff_file?: string
  stat?: Array<[string, number, number]>
  dry_run_id?: string | null
  undo_id?: string | null
  applied?: boolean
  problems?: string[]
}

export type FileDiff = {
  path: string
  /** `---`/`+++` and rename lines, before the first hunk. */
  header: string[]
  /** Each hunk with its `@@` line first. */
  hunks: string[][]
  /** A binary or symlink change: pyedit renders it as one line, no hunks. */
  note?: string
}

const NOTE_PREFIXES = ['Symlink ', 'Binary file ']
const HUNK = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/

function isResult(value: unknown): value is RunResult {
  return typeof value === 'object' && value !== null && typeof (value as RunResult).ok === 'boolean'
}

/**
 * The tool's output as the transcript stores it: the result as JSON text,
 * or an MCP result whose text blocks hold it. Text that is no result, such
 * as an error the harness wrote, comes back as it is.
 */
export function readOutput(output: unknown): RunResult | string | undefined {
  if (isResult(output)) return output
  let text: string | undefined
  if (typeof output === 'string') {
    text = output
  } else if (Array.isArray(output)) {
    text = output.map(block => (block as { text?: string }).text ?? '').join('')
  } else if (typeof output === 'object' && output !== null && Array.isArray((output as { content?: unknown }).content)) {
    const result = output as { content: Array<{ text?: string }>; structuredContent?: unknown }
    if (isResult(result.structuredContent)) return result.structuredContent
    text = result.content.map(block => block.text ?? '').join('')
  }
  if (text === undefined) return undefined
  try {
    const parsed: unknown = JSON.parse(text)
    if (isResult(parsed)) return parsed
  } catch {
    // Not JSON: an error text.
  }
  return text
}

function pathOf(header: string[]): string {
  const to = header.find(line => line.startsWith('+++ '))?.slice(4)
  const from = header.find(line => line.startsWith('--- '))?.slice(4)
  const path = to && to !== '/dev/null' ? to : (from ?? '')
  return path.replace(/^[ab]\//, '')
}

/**
 * One section per file. A hunk ends by its `@@` line counts, not by a
 * prefix: a removed line `-- x` reads `--- x`, like a file header.
 */
export function splitDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = []
  let current: FileDiff | undefined
  let oldLeft = 0
  let newLeft = 0
  for (const line of diff.split('\n')) {
    if (oldLeft > 0 || newLeft > 0) {
      current?.hunks.at(-1)?.push(line)
      if (line.startsWith('\\')) continue
      if (!line.startsWith('+')) oldLeft--
      if (!line.startsWith('-')) newLeft--
      continue
    }
    const hunk = HUNK.exec(line)
    if (hunk && current) {
      oldLeft = hunk[1] === undefined ? 1 : Number(hunk[1])
      newLeft = hunk[2] === undefined ? 1 : Number(hunk[2])
      current.hunks.push([line])
      continue
    }
    if (line.startsWith('\\')) {
      current?.hunks.at(-1)?.push(line)
      continue
    }
    const note = NOTE_PREFIXES.find(prefix => line.startsWith(prefix))
    if (note) {
      const path = line.slice(note.length).split(' ')[0] ?? ''
      files.push({ path, header: [], hunks: [], note: line })
      current = undefined
      continue
    }
    // `---` opens a file, unless it follows the `diff --git` lines of a rename.
    const inHeader = current !== undefined && current.hunks.length === 0
    const opens =
      line.startsWith('diff --git ') ||
      (line.startsWith('--- ') && !(inHeader && !current?.header.some(l => l.startsWith('--- '))))
    if (opens) {
      current = { path: '', header: [], hunks: [] }
      files.push(current)
    } else if (!inHeader) {
      continue
    }
    if (!current || line.startsWith('diff --git ')) continue
    current.header.push(line)
    current.path = pathOf(current.header)
  }
  return files.filter(file => file.note !== undefined || file.header.length > 0)
}

/**
 * The section as `Code` source: whole hunks while they fit in `budget`
 * lines, and always the first one. `hidden` counts the lines left out.
 */
export function shown(file: FileDiff, budget: number): { source: string; hidden: number } {
  const kept: string[][] = []
  let used = 0
  let hidden = 0
  for (const hunk of file.hunks) {
    if (kept.length === 0 || used + hunk.length <= budget) {
      kept.push(hunk)
      used += hunk.length
    } else {
      hidden += hunk.length
    }
  }
  const header = file.header.filter(line => line.startsWith('--- ') || line.startsWith('+++ '))
  return { source: [...header, ...kept.flat()].join('\n'), hidden }
}

/** Lines added and removed, counted from the hunks. */
export function counts(file: FileDiff): [number, number] {
  let added = 0
  let removed = 0
  for (const line of file.hunks.flatMap(hunk => hunk.slice(1))) {
    if (line.startsWith('+')) added++
    else if (line.startsWith('-')) removed++
  }
  return [added, removed]
}
