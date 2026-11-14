import { describe, expect, test } from 'claude-code/testing'

import { counts, readOutput, shown, splitDiff } from './diff'

// What pyedit's MCP run tool returned for a two-file dry run, as stored.
const OUTPUT = JSON.stringify({
  ok: true,
  diff: '--- a/README.md\n+++ b/README.md\n@@ -1,3 +1,3 @@\n # Demo\n \n-Says hi.\n+Says hello.\n--- a/app.py\n+++ b/app.py\n@@ -1,5 +1,5 @@\n def greet():\n-    print("Hi")\n+    print("Hello")\n \n \n def main():\n',
  stat: [['README.md', 1, 1], ['app.py', 1, 1]],
  dry_run_id: 'e23f0995',
  undo_id: null,
  applied: false,
  problems: [],
})

describe('readOutput', () => {
  test('reads the JSON text the transcript stores', () => {
    const result = readOutput(OUTPUT)
    expect(typeof result === 'object' && result.dry_run_id).toBe('e23f0995')
  })

  test('reads the text blocks of an MCP result', () => {
    const result = readOutput({ content: [{ type: 'text', text: OUTPUT }], isError: false })
    expect(typeof result === 'object' && result.ok).toBe(true)
  })

  test('gives other text back as it is', () => {
    expect(readOutput('MCP error -32001: Request timed out')).toBe('MCP error -32001: Request timed out')
  })
})

describe('splitDiff', () => {
  test('cuts one section per file, with the path from +++', () => {
    const files = splitDiff((readOutput(OUTPUT) as { diff: string }).diff)
    expect(files.map(file => file.path)).toEqual(['README.md', 'app.py'])
    expect(files.map(counts)).toEqual([[1, 1], [1, 1]])
  })

  test('keeps a removed line that reads like a file header inside its hunk', () => {
    const files = splitDiff('--- a/x.md\n+++ b/x.md\n@@ -1,2 +1,1 @@\n--- a/rule\n keep\n')
    expect(files.length).toBe(1)
    expect(files[0]?.hunks[0]).toEqual(['@@ -1,2 +1,1 @@', '--- a/rule', ' keep'])
  })

  test('takes the old path of a deleted file, and a note as its own section', () => {
    const files = splitDiff('--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\nBinary file logo.png changed (10 -> 12 bytes)\n')
    expect(files.map(file => file.path)).toEqual(['gone.py', 'logo.png'])
    expect(files[1]?.note).toBe('Binary file logo.png changed (10 -> 12 bytes)')
  })

  test('reads a pure rename as a section with no hunks', () => {
    const files = splitDiff('diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n--- a/a.py\n+++ b/b.py\n')
    expect(files.map(file => [file.path, file.hunks.length])).toEqual([['b.py', 0]])
  })
})

describe('shown', () => {
  test('keeps whole hunks within the budget and counts the rest', () => {
    const [file] = splitDiff('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d\n')
    if (!file) throw new Error('no section')
    expect(shown(file, 3)).toEqual({ source: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b', hidden: 3 })
    expect(shown(file, 100).hidden).toBe(0)
  })
})
