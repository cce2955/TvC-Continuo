# Yami stage carousel research (2026-10-18)

Scope: Continuo runtime changes only. Do not edit the game ISO or its FPKs.
The existing Stage Control **Test Yami arena via Workshop** route works: choose
Workshop, continue through character select, and the match loads Ark of Yamato
(arena result ID 15). This route is separate from adding a native stage tile.

## Observed menu state

- In this Dolphin session, `*(0x809BCE38)` pointed to loaded
  `fpack/menu/001/0000.fpk` / `menu/main/chrsel.seq` at `0x908183C0`.
  The base is dynamic; never hard-code it.
- Stage carousel object: `*(0x809BCE28)`; live cursor at object `+0x6C`, global
  cursor at `0x809BCEE0`. Stage 14 is Willamette, 15 is hidden Ark of Yamato,
  17 is Training, and 0 is Random. Native navigation skips 15.
- At sequence offset `0x26103`, a guarded byte `0F -> 10` changes the visible
  tile at cursor 17 to Ark. This **replaces Training** and is not a valid final
  solution. Changing adjacent offset `0x26113` from `10 -> 11` still replaced
  Training.
- With the display byte changed, three navigation bytes at `0x25B7F`,
  `0x25B87` (`0F -> 11`), and `0x25BAB` (`0F -> 10`) produced Down navigation
  `14 -> 15 -> 17 -> 0`. The player visually confirmed Ark and Training as
  separate tiles, but reverse navigation still skipped Ark or stopped.
- Symmetric-looking reverse changes at `0x25CDB`, `0x25CE3`, and `0x25D07`
  were tried in combinations of `0F -> 10/11`. A reverse-boundary test with
  `0F -> 0E` at `0x25CDB` and `0x25CE3` also did not show cursor 15 in the
  live cursor trace, even though the player pressed Up from Training. None
  produced reliable Up navigation. Do not enable the partial patch in a build.
- A later guarded probe kept the reverse bytes stock while applying the four
  working forward/display changes. The live cursor moved `17 -> 0` when Up
  was pressed from Training. The same trace showed `15 -> 14` on Up from Ark,
  so the failure is specifically the Training boundary. Changing reverse
  operands `0x25CDB` and `0x25CE3` from `0F -> 12` still yielded `17 -> 0`.
  The probe restored every byte afterward.
- The stage-select script signature around offset `0x260F0` in the stock
  archive is:
  `0401130301450000040104030402033f0000000f013700000002611c0401043f00000010`.
  Verify the package path and surrounding bytes before any live write.

All temporary live menu bytes were restored and verified as original on
2026-10-18. No FPK or ISO was written. The earlier partial script patch was
withheld; the guarded runtime implementation below was added afterward.

## Runtime implementation after the probes

The source now has an opt-in Ark tile in Extra Stages. It patches only the
four verified forward/display SEQ bytes while the native carousel is live,
restores them when that scene closes or the toggle turns off, and refuses an
unexpected script layout. Down reaches cursor 15 (Ark) and cursor 17
(Training) separately. Cursor 15 auto-arms the existing arena handoff through
character select; cursor 17 stays Training. Up from Training still skips Ark;
the Extra Stages window warns users to use Down. An experimental input-based
reverse correction was removed because it was not confirmed live.
The opt-in checkbox starts Off on every launch, including when an older
`yami_tile.json` contains `enabled: true`.

The guard, restoration, and ID-15 load handoff have automated tests. The
user stopped the repeated manual reverse-boundary probes. Do not claim full
in-game validation of this source implementation.

## Next useful investigation

Trace the stage-navigation branches in `chrsel.seq` around `0x25B60` and
`0x25CC0`, including the event/condition semantics of `0132`, `0133`, `0134`,
and `013C`. The apparent mirror blocks did not behave symmetrically in live
  tests. Cursor 15 already has a guarded arena handoff; the remaining issue is
  reverse navigation. Keep the toggle default Off and restore all patched
  bytes when disabled.
