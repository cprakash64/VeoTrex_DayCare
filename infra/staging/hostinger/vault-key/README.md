# Vault master key custody

A database archive alone does not recover this system. Ring credentials are stored as AEAD
ciphertext bound to the vault master key, so a restored dump without the matching key yields rows
that are present and undecryptable.

The archive half is proven: an encrypted archive has been restored into an isolated PostgreSQL
instance and structurally validated (H2C-R1 — `pg_restore` exit 0, zero error lines, migration
head, table, RLS-policy and row-count parity, isolated volume removed, live volume untouched).

This directory closes the other half: a bounded, rehearsed procedure that puts an **encrypted**
copy of the **existing** vault master key somewhere the VPS cannot reach.

## What this procedure deliberately does not do

- **It does not rotate the key.** Rotation invalidates every archive taken before it and is a
  migration, not a setting. The live key file is left byte-for-byte unchanged.
- **It never reveals the key.** The key is not printed, echoed, passed on argv, placed in the
  environment, written to shell history, or copied in plaintext. `age` reads the secret file
  directly by path; the bytes never pass through a shell variable or a terminal.
- **It does not reuse the database-backup identity.** A single identity that unlocks both the
  archives and the key that makes them readable is one compromise away from total loss. The gate
  refuses if the two recipients are the same key — and refuses just as hard if the backup
  recipient file is missing, unreadable, a directory, empty, or holds no `age1` recipient.
  "Unreadable" is not "different": every way of failing to answer the question is a refusal.
- **It leaves no private recovery material on the VPS.** The host receives a public age
  recipient and nothing else. The gate refuses any file containing `AGE-SECRET-KEY-`, and the
  verifier refuses to run on a machine that carries the marks of this host.
- **It touches no Ring material.** The gate reads one file: the vault master key.

| Component | Path |
| --- | --- |
| Root gate (runs on the VPS) | `veotrex-vault-key-escrow.sh` → `/usr/local/sbin/veotrex-vault-key-escrow` (root:root 0700) |
| Verifier (runs off-host only) | `veotrex-vault-key-verify.sh` |
| Combined custody run | `veotrex-vault-key-stage.sh` → `/usr/local/sbin/veotrex-vault-key-stage` (root:root 0700) |
| Live vault key | resolved from `VEOTREX_HOSTINGER_SECRETS_DIR` in `/etc/veotrex-daycare/hostinger.env` |
| Public recovery recipient (on VPS) | `/etc/veotrex-daycare/vault-recovery-recipient.txt` |
| Escrow artefact (transient, on VPS) | `/root/veotrex-vault-escrow/veotrex-vault-master-key-<UTC>.age` |
| Private recovery identity | **off-host only**, passphrase-wrapped, never on the VPS |

The gate does not carry its own idea of where the secrets live. It reads
`VEOTREX_HOSTINGER_SECRETS_DIR` from the deployment env file, which is the same file compose
interpolates, so the key it escrows is by construction the key the running stack mounts. This
matters here: Docker is installed as a snap and cannot bind-mount arbitrary host paths, so the
secrets directory is under `/var/snap/docker/common/`, not the `/etc/veotrex-daycare/secrets`
that `hostinger.env.example` shows. A gate with a hardcoded default would have escrowed nothing.

Like the backup script, the gate is executed from `/usr/local/sbin` and never from
`/srv/veotrex-daycare`: that checkout is owned by the unprivileged `veotrex` user, and a root
command running a file that user can edit is a privilege-escalation path.

---

## 0. VPS — the whole run, one command

`veotrex-vault-key-stage.sh` performs the six phases in the only safe order and aborts before
the next phase if one fails: install the pinned gate, verify the installation, baseline the
running deployment, install and validate the public recovery recipient, escrow the key, report.
It takes the public recovery recipient as its only argument.

It is rerunnable. If an escrow artefact already exists it is reported, never replaced — two
ciphertexts of one key double the exposure and halve the clarity of custody.

When it is waiting for you it prints `PHASE5_CONFIRMATION_REQUIRED` and the exact phrase to type,
and prints `PHASE5_CONFIRMATION_ACCEPTED` once you have. Those markers exist because a run that
died between two phases was indistinguishable from one that was waiting for a human. Any
non-zero exit now names the phase it died in — silence is not a possible outcome.

The operator command fast-forwards the checkout as the deployment user, pins both the commit and
the script's checksum, installs the orchestrator root-owned, and executes only the root-owned
copy. Root never executes a file the `veotrex` user can edit.

Steps 1 and 3 below are what that script automates; run them by hand only when diagnosing.

## 1. VPS — install the gate, checksum-pinned

The repository is the source of truth; the operational copy is a root-owned installation of a
**committed** file. Never run this gate from the checkout, and never run an uncommitted version
of it against the live key.

```bash
git -C /srv/veotrex-daycare/repo fetch --all && git -C /srv/veotrex-daycare/repo status --porcelain   # must be empty
sha256sum /srv/veotrex-daycare/repo/infra/staging/hostinger/vault-key/veotrex-vault-key-escrow.sh

install -o root -g root -m 0700 \
    /srv/veotrex-daycare/repo/infra/staging/hostinger/vault-key/veotrex-vault-key-escrow.sh \
    /usr/local/sbin/veotrex-vault-key-escrow

# Installed copy must be byte-identical to the qualified source, and unwritable by `veotrex`.
sha256sum /usr/local/sbin/veotrex-vault-key-escrow
stat -c '%U:%G %a' /usr/local/sbin/veotrex-vault-key-escrow     # root:root 700
sudo -u veotrex test -w /usr/local/sbin/veotrex-vault-key-escrow && echo "WRITABLE — STOP"
```

Reinstall after every change to the script in Git, and re-compare the checksums. A drifted
installed copy is an unreviewed root script.

## 2. Off-host — create a dedicated recovery identity

On the trusted operator machine. Nothing in this step touches the VPS, and the private half never
will.

```bash
umask 077
mkdir -p ~/veotrex-vault-recovery && cd ~/veotrex-vault-recovery

age-keygen -o recovery-identity.txt          # private  — never leaves this machine
age-keygen -y recovery-identity.txt > recovery-recipient.txt   # public — may go to the VPS
```

Confirm it is **not** the database-backup key pair, and not any other identity in play:

```bash
diff <(cat recovery-recipient.txt) <(cat ~/veotrex-backup/backup-age-recipient.txt) && \
  echo "SAME KEY — regenerate, do not proceed"
```

Wrap the private identity at rest, prove the wrapped copy opens, then destroy the bare copy:

```bash
age -p -o recovery-identity.txt.age recovery-identity.txt
age -d recovery-identity.txt.age | grep -q 'AGE-SECRET-KEY-' && echo "wrapped identity recovers"
shred -u recovery-identity.txt
```

Store, off-host and in two places that do not fail together:

- `recovery-identity.txt.age` — password manager attachment **and** an encrypted external drive;
- the passphrase — in the password manager entry, not beside the file it unlocks;
- and **not** alongside the escrow artefact from step 4. Two halves, two custodial locations.

## 3. VPS — install the public recipient, then run the manual root gate

```bash
install -d -o root -g root -m 0700 /etc/veotrex-daycare
install -o root -g root -m 0600 recovery-recipient.txt /etc/veotrex-daycare/vault-recovery-recipient.txt

sudo /usr/local/sbin/veotrex-vault-key-escrow
```

`recovery-recipient.txt` is a public key. It is the only piece of recovery material this host is
ever allowed to hold.

The gate is interactive on purpose: an escrow copy of the master key is a custody event a human
authorises at the moment it happens, so there is no unattended or timer-driven path. Before
writing anything it checks that it is root, that the key file is present, mode 0600 and decodes to
32 bytes, that the recipient is exactly one public `age1…` key, that the recipient is not the
backup recipient, and that no artefact would be overwritten — then prints a summary and requires
the phrase `ESCROW VAULT KEY`. Any refusal writes nothing at all.

It reports a **fingerprint**: the first 16 hex characters of the SHA-256 of the key file. That is
a commitment to the key, not the key — 32 random bytes behind a preimage-resistant hash — and it
exists so step 4 can prove the recovered bytes are the ones this host is running without either
side transmitting the key.

## 4. Off-host — verify by actually decrypting

An escrow copy that has not been decrypted is not a backup, it is a hope.

```bash
scp root@<vps>:/root/veotrex-vault-escrow/veotrex-vault-master-key-<UTC>.age .
scp root@<vps>:/root/veotrex-vault-escrow/veotrex-vault-master-key-<UTC>.age.manifest .

./veotrex-vault-key-verify.sh \
    ./veotrex-vault-master-key-<UTC>.age \
    ~/veotrex-vault-recovery/recovery-identity.txt.age \
    <fingerprint from step 3>
```

The verifier decrypts for real, compares the fingerprint, re-checks the 32-byte decoding, and
runs an AES-256-GCM round-trip with the recovered key. It prints a verdict and never the key. The
plaintext lives only inside a RAM-backed work directory and is shredded on every exit path,
including interruption.

It refuses outright to run on a machine carrying the marks of the VPS. It needs the private
identity, and that identity must never exist on the host it protects.

## 5. VPS — remove the transient copy, then record custody

Only after the verifier prints `PASS`:

```bash
sudo shred -u -- /root/veotrex-vault-escrow/veotrex-vault-master-key-<UTC>.age \
                 /root/veotrex-vault-escrow/veotrex-vault-master-key-<UTC>.age.manifest
```

Record in the password manager entry, beside the wrapped identity: the fingerprint, the date, and
that the key is the one in force from that date. That date is what tells a future operator which
archive generations this key can actually open.

---

## When custody is qualified

All four, or it is not:

1. the escrow artefact exists off-host, in two locations that do not fail together;
2. the private recovery identity exists off-host, passphrase-wrapped, and has never been on the VPS;
3. the artefact has been **decrypted off-host** and its fingerprint matched the live key;
4. no copy of the artefact remains on the VPS.

## Two rehearsals, not one

Do not conflate these:

| State | Question | Status |
| --- | --- | --- |
| `RESTORE_REHEARSAL` | Can an application-consistent archive be restored and structurally validated? | **QUALIFIED** (H2C-R1) |
| `CREDENTIAL_RECOVERY_REHEARSAL` | Can a restored encrypted credential row be decrypted with the recovered vault master key? | **PENDING** |

The second is pending, not failed: the staging vault holds no real credential row yet, so there is
nothing to decrypt. It is closed in one of two ways — a synthetic exercise that writes a
throwaway credential through `EncryptedCredentialVault`, restores it from an archive into a
scratch instance and decrypts it with the recovered key; or the same drill against the first real
credential once the Ring portal gate is complete. Until one of them has been run, end-to-end
recovery of this system is designed and unproven.

Rotation invalidates every archive taken before it. If the key is ever rotated, this procedure is
re-run for the new key and the old escrow artefact is retained for as long as archives encrypted
under the old key are retained.
