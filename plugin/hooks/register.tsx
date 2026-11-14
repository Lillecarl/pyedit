import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderInput, RenderNode } from 'claude-code'

import { counts, readOutput, shown, splitDiff } from './diff'
import type { RunResult } from './diff'

// pyedit's MCP server is `pyedit`: `mcp__pyedit__run` from settings,
// `mcp__plugin_<plugin>_pyedit__run` from a plugin's .mcp.json.
const PYEDIT_RUN = /^mcp__(?:plugin_[^_]+_)?pyedit__run$/
// Lines of diff a file shows in the folded transcript; ctrl+o shows all.
const BUDGET = 30

const collapsed = atom({ plugin: 'pyedit-diffs', key: 'collapsed' } as const, {}, { shape: 'collapsed-1' })

function isPyeditRun(tool: string): boolean {
  return PYEDIT_RUN.test(tool)
}

function status(result: RunResult): { text: string; color: string } {
  if (!result.ok) return { text: result.error ?? 'pyedit failed', color: 'error' }
  if (result.applied) {
    return { text: `Applied${result.undo_id ? ` · undo with apply_id ${result.undo_id}` : ''}`, color: 'success' }
  }
  if (result.diff_file) return { text: `Diff written to ${result.diff_file}`, color: 'subtle' }
  if (!result.diff) return { text: 'No changes', color: 'subtle' }

  return { text: `Dry run · apply with apply_id ${result.dry_run_id ?? '?'}`, color: 'warning' }
}

function body(
  $: EngineInterface,
  e: RenderInput,
  id: string,
  result: RunResult,
  closed: Record<string, true>,
  budget: number,
): RenderNode {
  const { Box, Button, Text, Code } = $.ui.resolve(e)
  const files = splitDiff(result.diff ?? '')
  const said = status(result)

  return (
    <Box flexDirection="column" paddingLeft={2}>
      <Text color={said.color}>⎿ {said.text}</Text>
      {files.map(file => {
        if (file.note !== undefined) return <Text dimColor>{file.note}</Text>
        const [added, removed] = counts(file)
        const { source, hidden } = shown(file, budget)
        const key = `${id}:${file.path}`
        const isOpen = !closed[key]
        const toggle = () =>
          void update($, collapsed, map => {
            const { [key]: _, ...rest } = map

            return isOpen ? { ...rest, [key]: true as const } : rest
          })

        return (
          <Box flexDirection="column" borderStyle="round" borderColor="subtle" paddingX={1}>
            <Button key={key} plain onPress={toggle}>
              <Text weight="bold">
                {isOpen ? '▾' : '▸'} {file.path}
              </Text>{' '}
              <Text color="success">+{added}</Text> <Text color="error">-{removed}</Text>
            </Button>
            {isOpen && file.hunks.length > 0 && <Code source={source} path={file.path} format="diff" />}
            {isOpen && hidden > 0 && <Text dimColor>… {hidden} more lines (ctrl+o)</Text>}
          </Box>
        )
      })}
      {(result.problems ?? []).map(problem => (
        <Text color="error">{problem}</Text>
      ))}
    </Box>
  )
}

export const register: Register = on => {
  // The transcript folds MCP calls into one line (`Called pyedit`). The boxes
  // go under that line: unfolding the group would print each raw JSON result.
  on('ui.render', { component: 'ToolGroup' }, async ($, e, next) => {
    const line = await next(e)
    if (e.props.isExpanded) return line
    const runs = e.props.calls.flatMap(call => {
      if (!isPyeditRun(call.tool) || call.isRunning || call.tool_use_id === undefined) return []
      const result = readOutput(call.output)

      return typeof result === 'object' ? [{ id: call.tool_use_id, result }] : []
    })
    if (runs.length === 0) return line
    const { Box } = $.ui.resolve(e)
    const closed = await read($, collapsed)

    return (
      <Box flexDirection="column">
        {line}
        {runs.map(run => body($, e, run.id, run.result, closed, BUDGET))}
      </Box>
    )
  })

  // The box says what the raw JSON says; the folded view drops the JSON, ctrl+o keeps it.
  on('ui.render', { component: 'ToolResult' }, async ($, e, next) => {
    if (!isPyeditRun(e.props.tool) || e.props.isExpanded || e.props.isErrored) return next(e)
    if (typeof readOutput(e.props.output) !== 'object') return next(e)
    const { Box } = $.ui.resolve(e)

    return <Box />
  })

  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const row = await next(e)
    if (!isPyeditRun(e.props.tool) || e.props.isRunning) return row
    const result = readOutput(e.props.output)
    if (result === undefined || typeof result === 'string') return row
    const { Box } = $.ui.resolve(e)

    return (
      <Box flexDirection="column">
        {row}
        {body($, e, e.props.tool_use_id, result, await read($, collapsed), e.props.isExpanded ? Infinity : BUDGET)}
      </Box>
    )
  })
}
