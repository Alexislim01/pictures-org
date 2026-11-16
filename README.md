# pictures-org

A local-first tool for gathering scattered photo and video collections, finding exact duplicates, and cleaning up with a way back. Processing stays on your computer. Version 0.1 is a command-line tool for Windows, with no runtime dependencies beyond Python 3.11 or newer.

**Implemented:** read-only inventory, resumable exact-duplicate scanning, CSV/JSON/Markdown reports, keeper proposals, recoverable quarantine, restore, and explicit permanent deletion.

**Later:** a visual interface for approving groups of similar shots, local academic-clutter classification, and organizing keepers by capture date and location. Similarity detection and cloud/phone importing are not implemented yet. The tool does not upload, compress, or delete anything from Google or your phone.

## Start on Windows

Download this repository using GitHub's **Code → Download ZIP**, extract it, and open PowerShell in the extracted folder. Alternatively, clone the repository if you use Git. Check Python:

```powershell
python --version
python -m pictures_org --help
```

No installation or API key is needed when running from this directory. If your Python launcher is `py`, substitute `py -3` for `python`. An optional package install is supported with `python -m pip install -e .`, which may download build tools; it is not required.

## 1. Gather files by copying

Copy phone and PC folders into a collection folder. Preserve source subfolders so identical names don't overwrite one another. For example:

```text
V:\pic_project\
    Phone\DCIM\
    Phone\SHAREit\
    PC\Pictures\
    GoogleExport\
```

Google Photos and Google Drive files are included only once downloaded into that collection. Google export sidecar metadata is inventoried as non-media, but is not parsed yet. Keep source copies until imported files have been verified. A single PC drive is not a backup.

Finish copying before scanning. Close programs that might rename, edit, or synchronize the collection during cleanup. Do not use a synced quarantine folder or operate on the same collection from another tool at the same time.

## 2. Scan without changing your media

```powershell
python -m pictures_org scan "V:\pic_project" --workspace ".pictures-org"
```

The workspace must be outside the collection and must not contain the collection. It holds:

| File | Purpose |
| --- | --- |
| `inventory.csv` | File paths, extensions, sizes, timestamps, media/other category |
| `report.md` | Readable summary and scan errors/exclusions |
| `duplicates.csv` | Every path in each exact-duplicate group |
| `report.json` | Machine-readable duplicate evidence |
| `hashes.sqlite` | Saved sample/full hashes for interrupted and repeated scans |

The scan groups equal-size files, compares sampled regions to eliminate non-matches, then verifies remaining matches with full SHA-256 hashes. Samples are never accepted as proof of duplication. Files that change during hashing are reported as errors. Symlinks/junctions are excluded, and multiple hardlink names are not counted as independent copies. Empty files are inventoried but not proposed for removal.

Press **Ctrl+C** to interrupt. Run the same command again to reuse saved hashes. Cache keys include path, size, modification time, change/creation time, and filesystem identity. Use `--fresh` to bypass the cache and re-read all candidates. These metadata checks detect routine changes; they are not protection against a program deliberately altering files while preserving metadata.

Large videos can take significant time to read. Progress includes bytes read within large files. GiB means 1,073,741,824 bytes, matching Windows' usual displayed size convention. Savings are estimates of logical file bytes; compression, sparse files, storage allocation, and snapshots can change actual freed space.

## 3. Create and review a cleanup plan

```powershell
python -m pictures_org plan --report ".pictures-org\report.json" --quarantine "V:\pictures-org-quarantine" --out ".pictures-org\cleanup-plan.json"
```

This writes a proposal. **It does not move files.** It includes recognized image/video files only; installers, documents, temporary files, and other non-media stay in place even if duplicated.

Open `cleanup-plan.json` to review the `source` (extra copy), `keeper` (retained file), and `destination` paths. By default the keeper preference is: filenames without “Copy” or numbered suffixes, shallower paths, shorter filenames, then alphabetical order. This does not infer which file has the best filesystem timestamps or external metadata associations. Embedded metadata is identical when full file contents are identical; external sidecars may differ and are not moved.

To prefer a known original folder, create a new plan filename:

```powershell
python -m pictures_org plan --report ".pictures-org\report.json" --quarantine "V:\pictures-org-quarantine" --out ".pictures-org\preferred-plan.json" --prefer "V:\pic_project\Phone"
```

The preferred folder must be within the collection. Do not hand-edit action paths or remove/reorder actions; quarantine slots and journal states are validated. Regenerate the proposal if you want different keepers. Per-group selection will come with the review interface.

## 4. Apply the reviewed plan

```powershell
python -m pictures_org apply --plan ".pictures-org\cleanup-plan.json"
```

Extra copies move into a unique, journaled quarantine folder **on the same drive**. The original keeper and each extra copy are content-verified before moving. Operations refuse links, invalid paths, occupied destinations, or changed files. The collection and plan are locked against concurrent cleanup runs.

**Quarantine does not free space on that drive.** It provides an undo period until you explicitly purge. The app has no scheduled or automatic deletion. Keep the JSON plan: it records original locations and is needed for restoration. The quarantine stores files under numbered slots, not their original names.

## 5. Restore if needed

```powershell
python -m pictures_org restore --plan ".pictures-org\cleanup-plan.json"
```

Files return to their original paths. Restore refuses to overwrite a newly created file and verifies quarantined content. It works even if a keeper is now missing. It cannot restore already-purged copies; it skips those with a message and restores remaining recoverable files. Repeating a completed operation is safe. A restored plan is not reapplied; rescan and create a new plan if you want to clean up again.

## 6. Permanently reclaim space

Only after reviewing your keepers and retaining a backup where needed:

```powershell
python -m pictures_org purge --plan ".pictures-org\cleanup-plan.json" --confirm "PLAN-ID-FROM-THE-JSON"
```

Replace the placeholder with the exact plan `id`. This permanently deletes quarantined copies, bypassing Windows Recycle Bin. The surviving keeper is reverified before deletion. A missing/modified keeper or modified quarantine file stops the operation. Once purged, these copies cannot be recovered using the tool.

This is deduplication within the local collection. It does not verify or clear the original phone/cloud folders, nor make a second backup of unique memories.

## Interrupted operations

Completed hashes and cleanup action states are saved incrementally. If interrupted between a file move and its journal update, rerun the same command: the tool checks both locations and reconciles the journal. Ambiguous states stop for inspection instead of guessing. An error can leave earlier actions completed; use the same plan to resume or restore.

A normal error or Ctrl+C releases locks. If a process is killed or the computer crashes, a `.lock` file may remain. First verify that **no pictures-org cleanup process is still running**, then remove only the lock files named in the error (the plan's `.lock` and/or the collection's `.pictures-org-cleanup.lock`) and retry. Do not delete the plan or quarantine files. The journal handles process interruption; it is not a guarantee against hardware failure, power-loss corruption, concurrent external edits, or loss of the PC drive.

## Development

```powershell
python -m unittest discover -s tests -v
```

Tests use disposable temporary folders and cover content identity, sample collisions, cache reuse/invalidation, keeper changes, quarantine/restore/purge, interrupted actions, hardlinks, path validation, and concurrency locks. Real symlink coverage skips where the OS does not permit creating links. CI is configured for Windows and Ubuntu with Python 3.11/3.12.

No user media, personal scan reports, API keys, or local state belong in this repository. Default state lives in the gitignored `.pictures-org` directory. If using another workspace, keep it outside the code repository or add it to `.gitignore`.
