/** The boxes the person closed, by `<tool_use_id>:<path>`. */
export type Collapsed = Record<string, true>

declare module 'claude-code' {
  interface PluginState {
    'pyedit-diffs': {
      collapsed: Shaped<Collapsed>
    }
  }
}
