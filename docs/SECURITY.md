# Data protection and privacy

Encryption at rest, key storage, recovery and limits. Back to the [README](../README.md).

## Data protection (encryption at rest)

**Goal.** Opening the data file with a normal SQLite browser, copying it in a
backup, or syncing `~/.local/share` to somewhere else must not expose what you
used or when. This is *authenticated encryption* (AES-256-GCM), not a hash:
a hash cannot hide data. (Hashes are used only as identifiers, such as the key
fingerprint, never as protection.)

### How it works

* `screentime.sec` is still a SQLite file, so it keeps SQLite's atomic,
  durable, multi-process writes, but it contains **no readable tables**: only
  ciphertext blobs and a few non-secret fields (format tag, a random store id,
  a sealed key-check). Its `-wal`/`-shm` files hold only ciphertext too.
* Every change the app makes is one sealed record (zlib-compressed SQL
  statements + parameters, encrypted with AES-256-GCM, fresh random 96-bit
  nonce, subkey derived by HKDF-SHA256 from the master key and the store id).
  Each record's associated data binds it to the store id and its position, so
  records can't be edited, swapped, reordered, dropped from the middle or moved
  between stores without failing authentication. A wrong key is told apart from
  tampering by a sealed key-check.
* Each process rebuilds a normal **in-memory** SQLite database from the newest
  encrypted snapshot plus later records, so all the existing SQL (statistics,
  history, ...) works unchanged. `temp_store=MEMORY` stops SQLite spilling
  plaintext sorts to a temp file.
* The daemon periodically (every ~10 min, past ~20,000 records) compacts the
  log into a new encrypted snapshot so startup stays fast and the file does not
  grow without bound. Readers and writers that were behind detect the new
  snapshot and reload. Heartbeats are not coalesced (see above), so expect the
  log to grow by roughly 8 MB/day between compactions, bounded by the
  threshold.
* No SQLCipher, no AUR dependency: only `python-cryptography` (official repo).

### Where the key lives

A random 256-bit key (from `secrets`, never derived from anything predictable,
never hard-coded). In order of preference:

1. **System keyring** via the freedesktop Secret Service D-Bus API (KWallet,
   GNOME Keyring, KeePassXC, ...), talking D-Bus directly through Gio.
2. **Key file** `~/.config/screentime/keys/<store id>.key` (mode 0600, written
   atomically). Used when no keyring can be used *silently*. It is in the config
   directory on purpose, so backups or sync of the data directory alone do not
   carry the key, but it is **weaker**: anyone who can read your whole home
   directory gets both. Settings -> Security says so and offers **Move to
   keyring**, which only deletes the key file after the keyring has returned the
   exact key.

The daemon never shows a keyring dialog (it would pop up at login). If the
keyring is merely locked or still starting, the daemon **waits and retries,
tracking is paused**, and Settings -> Security shows "Paused: waiting for the
keyring" with an **Unlock** button (the GUI may prompt). With KWallet's PAM
integration the wallet is normally unlocked at login and you never see this.

**Recovery key.** Losing the key means losing the data, so Settings ->
Security shows a checksummed, human-copyable **recovery key** (Base32 groups;
catches any single-character typo). Save it somewhere safe and offline. If the
key is lost, ScreenTime explains the situation and lets you restore it (GUI
screen, or `screentime-security import-recovery-key`). A recovery key is only
accepted if it actually opens *this* database, so a typo or another machine's
key can never replace a working key.

```sh
screentime-security status                # what is protected, where the key lives
screentime-security verify                # authenticate every stored record
screentime-security export-recovery-key
screentime-security import-recovery-key   # prompts, keeping it out of shell history
screentime-security move-key-to-keyring
screentime-security compact
```

### Upgrading an existing install (migration)

On first start the new daemon (or GUI) finds the old plaintext
`screentime.db` and migrates it, under a cross-process lock so two starters
cannot both migrate:

1. integrity-check the old database and take a consistent image (including any
   uncheckpointed WAL content);
2. create the key and store it, build the encrypted store under a temporary
   name;
3. **verify**: reopen the new store using the key *fetched back from the
   keystore* and compare every table row-by-row (count + SHA-256) with the
   original; abort on any difference;
4. swap it in atomically, then overwrite with zeros and delete the plaintext
   `screentime.db`, `-wal`, `-shm` and `-journal`.

Any failure before step 4 leaves your original untouched and removes the
staging files. If the process dies between the swap and the deletion, the next
start finishes the job, but only if the plaintext file has not been modified
since the migration; if it has, it is kept and a warning is shown. Creating a
brand-new store is atomic too (built under a temporary name), so being stopped
mid-creation cannot leave a half-built store.

### What is and is not protected

Protected: the contents of `screentime.sec` at rest (app names, window
identifiers, times, durations, settings), including its WAL; against someone
who copies the file, browses it, or reads a backup of the data directory
without the key.

**Not protected, by design or by limits of the approach:**

* **While ScreenTime runs, the data is decrypted in memory**, and the key is
  available to it. Encryption cannot protect against malware running as you,
  a debugger, or anything that can read the process's memory or ask your
  unlocked keyring for the secret (Secret Service has no per-application
  access control in GNOME Keyring; KWallet may ask).
* **Offline attacker with your whole disk:** in keyring mode the strength is
  that of your wallet/login password; in key-file mode the key sits beside the
  data in your home directory, so encryption only helps if the data file is
  separated from the key. For stolen-laptop protection, use full-disk
  encryption (LUKS).
* **Rollback and truncation are not detected.** Authentication catches any
  *modification*, reordering or removal of records from the middle, but
  someone who can write the file can restore an older valid copy or drop the
  newest records. (A test pins this behavior.)
* **Metadata is visible:** file size, number of records, and when they were
  written (file times).
* **Old plaintext copies.** Secure deletion is best effort. Overwriting a file
  does not reach copies kept by copy-on-write filesystems (btrfs, ZFS), snapshot
  tools (snapper), SSD wear levelling, or journals. Backups or snapshots made
  *before* the migration still contain the plaintext. Full-disk encryption and
  pruning old snapshots are the real fix.
* **Swap, hibernation and core dumps** may write the in-memory database to
  disk; use encrypted swap and be aware of `coredumpctl`. Python cannot
  guarantee zeroing memory.
* **Downgrading** to a ScreenTime older than this feature cannot read the
  encrypted store and would start a new, empty plaintext database; Settings
  flags a plaintext `screentime.db` next to the store.
* **No key rotation, and no sync between machines.**

### Verification status

Tested here: the container (confidentiality, nonce reuse, every tamper case
above), key handling, the Secret Service client against a **real gnome-keyring**
in a private D-Bus session (store/load/replace/isolation, locked-keyring
behavior, a full create + migrate cycle with the keyring), migration including
failure injection, multi-process writers, `kill -9` mid-write, compaction under
concurrent readers, real-daemon upgrade from a plaintext install, and the whole
existing suite re-run on the encrypted backend. **Not tested here:** KWallet
itself (it exposes the same API, but I could not run it), and the full
"keyring locked at login, then unlocked later" flow against a real keyring
(only the wait-and-resume logic is tested, with simulated backends).

## Privacy

All data is stored locally in `~/.local/share/screentime/screentime.sec`,
encrypted at rest. This application makes **no network requests of any
kind**: no HTTP client, no update checker, no crash reporter, no analytics SDK,
no remote key server. No network module is imported anywhere in the package (a
test enforces this). `grep -r "socket\|requests\|urllib\|http" screentime/`
matches only two documentation URLs inside text strings. The only IPC is
D-Bus on your own machine: `logind`, the compositor helpers, and (for the key)
the local Secret Service.

