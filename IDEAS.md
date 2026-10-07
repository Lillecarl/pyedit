# AI ideas: jj in pyedit

Proposals from exploring pyjj's surface against pyedit's session model.
The shapes rhyme: VFS-discard ≈ `atomic()` rollback, scope-merge ≈
squash, checkpoint ≈ op log. pyedit thinks in transactions already;
it just speaks files so far.

Bindings already expose `set_author` / `set_committer` /
`set_description` / `set_parents`; the pyjj wrapper lacks
author/committer verbs (reachable via `tx.transaction` meanwhile).
Per-commit `annotate`, diff, revsets and op log are readable today.

1. **Metadata as a second staged channel.** Stage
   `(description, author)` per `-r` target next to tree bytes;
   `--apply` writes both through one `rewrite_commit` in the same
   transaction as the tree restore. Scopes touching one commit's
   message: equal values merge, differing values raise `Collision`
   (the binary-file rule). Builds toward 2 and 3. [DONE]
2. **One scope per commit: `with pyedit.commit("msg")`.** A scope
   becomes a child commit on clean exit, abandoned on error.
   Plan-shaped history with discard-on-error as the net. Shipped
   as insert-after-REV with automatic reparenting. [DONE]
3. **`-r` over a revset.** Same script against every commit in a
   revset: export, edit, amend, sequentially; stop loud on first
   conflict with the op id to restore.
4. **Readers: annotate + log.** `pyedit blame <rev> <path>` via the
   existing binding; compact log view. Almost free.
5. **`pyedit absorb`.** Stage in the WC, push each hunk to the
   ancestor that last touched those lines. Verb exists in pyjj.
6. **Checkpoint ↔ op log.** Name the op id next to each
   `EditSession` checkpoint so a later run can restore it. Weakest:
   two undo systems may confuse.
