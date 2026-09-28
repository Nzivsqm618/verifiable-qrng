# Verifiable Quantum Random Number Generator (`vqrng`)

[![Qiskit](https://img.shields.io/badge/Qiskit-1.x-6929C4?logo=qiskit&logoColor=white)](https://qiskit.org/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CLI Tool](https://img.shields.io/badge/CLI-vqrng-green.svg)](https://github.com/your-org/vqrng)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An open-source Python library and Unix CLI tool (`vqrng`) for generating unbiased random integers from quantum measurements with Qiskit, together with an evidence record that can be audited offline.

`vqrng` combines **HMAC-SHA256 conditioning**, **unbiased rejection sampling**, **tamper-evident JSON evidence** with optional **Ed25519 signatures**, and an optional **CHSH (Bell inequality) spot-check** run alongside the pool.

**⚠️ Status: Educational & Research-grade Verifiable QRNG Framework - Not ready for production cryptographic key generation.**

---

## Executive Summary & Goals

Classical Pseudo-Random Number Generators (PRNGs) and unverified Hardware RNGs rely on opaque physical noise or deterministic seed states. `vqrng` is a dual-interface (SDK + CLI) framework for studying verifiable quantum randomness:

* **Conditioning, then Unbiased Integer Conversion:** Raw measurement bits are conditioned with HMAC-SHA256, then mapped to `[min, max]` by strict rejection sampling (n<sub>bits</sub> = ⌈log<sub>2</sub>(range_size)⌉), so the mapping adds no modulo bias.
* **Tamper-Evident Provenance:** Records every raw shot, the conditioned candidates, rejected candidates, circuit hashes, and a canonical SHA-256 `pool_hash` for offline auditing. An optional Ed25519 signature ties the record to a key you trust.
* **Dual Execution Modes:** Local testing with the `qiskit-aer` simulator, and execution on physical IBM Quantum QPUs.
* **Graduated Verification Hierarchy:** Reproducible conversion (Level A), self-consistency and optional signature checks (Level B), and a near-real-time CHSH spot-check (Level C).

---

## Key Features

* **Python SDK & Unix Pipeline CLI:** Import `vqrng` directly inside Python projects or chain `vqrng` in Unix shell pipelines. Stdout is plain numbers by default, one per line. Pass `-r` for one space-separated line, or `-j` for one compact canonical JSON line.
* **HMAC-SHA256 Entropy Conditioning:** Every 512 raw bits are conditioned into 256 output bits before rejection sampling, using only the Python standard library (`hmac`, `hashlib`).
* **Strict Rejection Sampling:** Maps conditioned bits to any `[min, max]` range with no modulo bias.
* **QPU Budget Control:** `-t` stops `vqrng` from submitting another IBM job once the reported QPU time is used. It is not a cap IBM enforces, and queue time is recorded separately from QPU time.
* **Offline Verification Engine:** `vqrng verify` audits evidence files, detects edits made after generation, and checks signatures against keys you pass with `--trusted-key`.
* **CHSH Spot-Check:** Optional Bell test with non-orthogonal measurement bases (A<sub>0</sub>, A<sub>1</sub>, B<sub>0</sub>, B<sub>1</sub>), run in the same job as the first pool batch. Level C checks *S* > 2 and that the runs happened within 10 seconds of the pool, on the same backend.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/your-username/vqrng.git
cd vqrng

# Install library and CLI executable in editable mode
pip install -e .

# Add signing support (Ed25519 via the `cryptography` package)
pip install -e ".[sign]"
```

Verification, including signature checks, needs only the standard library. Only signing needs `cryptography`.

---

## Quick Start: CLI Usage

By default, `vqrng` writes plain random numbers to stdout (`74`, or one number per line for a pool) so the output is ready for shell scripts and pipes. Logs and status updates go to stderr. Pass `-r` / `--raw` to print a pool on one space-separated line, or `-j` / `--json` to write one compact canonical JSON line (sorted keys, no extra whitespace), the same encoding used for `pool_hash`.

Help is `--help`. `-h` selects IBM Quantum hardware, not help.

### 1. Basic Generation (Simulator Mode)

Generate a single random integer between 1 and 100 using Qiskit Aer:

```bash
vqrng -s 1 100
```

**The Aer backend (`-s`, the default) is a simulator.** Its "measurements" come from a C++ pseudo-random number generator (PRNG), not a quantum process. Use it for local testing and development only. Its output is not quantum randomness.

### 2. Generate a Random Pool

Generate a pool of 20 random numbers in the range [1, 1000]:

```bash
vqrng -s -p 20 1 1000
```

### 3. Run on IBM Quantum Hardware

Submit to physical IBM QPU hardware, and stop submitting further jobs after 300 seconds of reported QPU time:

```bash
export IBMQ_API_TOKEN="your_ibm_quantum_api_token"
vqrng -h -t 300 -p 10 1 100

# N-digit number on hardware, pinned to a specific QPU
vqrng -h -t 300 --backend ibm_torino -d 6 --pad
```

`vqrng` uses the least busy operational QPU unless `--backend` is given. Job status (`QUEUED`, `RUNNING`, `DONE`, `ERROR`, `CANCELLED`) is reported on stderr with timestamps, so stdout carries only the numbers or JSON. The evidence records `quantum_seconds` (QPU time reported by IBM), `charged_seconds` (what was counted against `-t`), `queue_seconds`, `wall_seconds`, the IBM `job_ids`, and each job's `started_at` / `finished_at` time. Pressing Ctrl+C while a job is waiting cancels it so it does not use more QPU time.

**`-t` is not an IBM billing cap.** Using `-h` prints a warning. IBM bills the QPU seconds a job actually uses, and one job can cost more than `-t` (a `-t 2` job can be billed 3 seconds). When that happens the evidence sets `"budget_exceeded": true` and `vqrng` warns on stderr. `-t` only stops `vqrng` from submitting a further job once the reported time is used up. `vqrng` does not set IBM's execution-time limit on the job: when that limit trips, IBM cancels the job and still charges the time already used, so the credit is spent and no numbers come back.

When IBM does not report a job's final QPU usage, `vqrng` counts the whole remaining `-t` budget against further jobs, so it does not submit another one. If generation stops early (budget exhausted, a job ends in `ERROR` or `CANCELLED`, or the job can no longer be polled), the command exits with status 1 and prints the job ids on stderr. With `-j`, it also prints the partial evidence (`"status": "partial"`) on stdout, so values and shots already paid for are kept.

### 4. JSON Evidence and Verification

Write the evidence payload, pipe it into the verifier, or audit a saved file. Verification requires the JSON payload, so generation commands that feed `vqrng verify` must include `-j`.

```bash
# One compact canonical JSON line on stdout
vqrng -s -j 1 100

# Pipeline verification
vqrng -s -j -p 50 1 1000 | vqrng verify

# File verification (UTF-8, or UTF-16 as written by Windows PowerShell's `>`)
vqrng verify evidence.json
```

`vqrng verify` reports each level separately and exits with status 1 if any level that ran fails:

```text
PASS: Level A (reproducible conversion)
PASS: Level B (tamper-evident provenance) via checksum only (self-consistent; not authenticated)
Level C (near-real-time CHSH spot-check): PASSED (S = 2.82)
```

Level C runs only when the evidence was generated with `--chsh`. Otherwise its line is `SKIP: Level C (near-real-time CHSH spot-check) was not run; the evidence has no chsh_data`.

The evidence `tape` records every raw measured shot of every job, including the unused tail of the last batch. `extractor` records the conditioning parameters. Each item's `bitstring` and `rejected` entries are conditioned candidates, and Level B recomputes them from the raw tape. `pool_hash` is the SHA-256 of the canonical JSON of the whole payload except `pool_hash`, `signature`, and `public_key`. Only evidence format version `"3"` verifies.

### 5. Signed Evidence

A checksum only shows that a record is self-consistent. Anyone who edits a record can recompute `pool_hash`. To tie evidence to its producer, sign it with an Ed25519 key, and have verifiers check it against the public key they already trust:

```bash
# Create a key once, keep signing.key secret, and publish the public key
python -c "from vqrng.signing import generate_signing_key as g; print(g())" > signing.key
python -c "from vqrng.signing import public_key_hex as p; print(p(open('signing.key').read()))"

# Sign while generating
vqrng -j --sign-key signing.key 1 100 > evidence.json

# Authenticate against the published key
vqrng verify --trusted-key <PUBLIC_KEY_HEX> evidence.json
```

The payload then carries `signature` and `public_key` (hex). Level B reports one of three outcomes:

| Level B outcome | Meaning |
| --- | --- |
| `via checksum only (self-consistent; not authenticated)` | Unsigned. The hashes match, but anyone could have produced the record. |
| `via Ed25519 signature from an untrusted key` | Validly signed, but by a key carried in the payload itself. A forger can sign with a key of their own, so this is no stronger than a checksum. |
| `via Ed25519 signature from a trusted key (authentic)` | Signed by a key passed with `--trusted-key`. |

With `--trusted-key`, Level B fails unless the evidence is signed by one of those keys.

### 6. N-Digit Numbers

Generate an N-digit number without setting `MIN_VAL` and `MAX_VAL` by hand:

```bash
# 6-digit number in [100000, 999999]
vqrng -s -d 6

# Zero-padded 6-character string from "000000" to "999999"
vqrng -s -d 6 --pad
```

---

## CLI Flags

### `-j`, `--json` (Evidence Payload Output)

Running `vqrng` without `-j` prints only plain random numbers. Supplying `-j` or `--json` prints one compact canonical JSON line (sorted keys, no extra whitespace). That is the same encoding used for `pool_hash`.

Use it for verification logging, audit trails, piping into `vqrng verify`, and storing provenance records. `-j` cannot be combined with `-r`.

### `-r`, `--raw` (Single-Line Output)

Prints the pool as space-separated values on one line, for example `741829 938201`. The default remains one value per line. `-r` cannot be combined with `-j`.

```bash
vqrng -s -r -p 3 1 100
```

### `--chsh` (Near-Real-Time CHSH Spot-Check)

Adds four Bell-state circuits, one per measurement setting (Alice at 0 or π/2, Bob at π/4 or −π/4), 1024 shots each, to the **first pool job**. On IBM hardware they are extra circuits in the same Sampler job, so they share its queue slot, calibration, and execution window. `chsh_data` records the outcome counts and, per setting, the job id, backend, and `started_at` / `finished_at` time. `pool_hash` covers all of it:

```bash
vqrng --chsh -j 1 100 | vqrng verify
```

Level C computes each correlation E = (N<sub>00</sub> + N<sub>11</sub> − N<sub>01</sub> − N<sub>10</sub>) / N and S = |E(A0,B0) + E(A0,B1) + E(A1,B0) − E(A1,B1)|. It passes when all of the following hold:

* S > 2, the classical limit. The quantum maximum is 2√2 ≈ 2.83.
* Every CHSH run executed on the same backend as the pool.
* Every CHSH run executed within 10 seconds of a pool batch. A run with missing or invalid times counts as decoupled and fails.

If the CHSH job fails, the command exits with status 1, and `chsh_data.error` says why.

### `--sign-key FILE`

Signs `pool_hash` with the hex Ed25519 private key seed in `FILE` and adds `signature` and `public_key` to the evidence. It needs `pip install vqrng[sign]`. A bad key is rejected before any job is submitted.

### `vqrng verify --trusted-key HEX`

Trusts the given hex Ed25519 public key. The flag can be repeated. When it is given, Level B passes only for evidence signed by one of these keys, and reports it as authentic.

### `--help`

Prints the generated usage text and exits. `-h` / `--hardware` selects IBM Quantum hardware and requires `-t` / `--runtime`. `-t` stops further jobs after that many reported QPU seconds. It does not cap what IBM bills for the job already submitted.

### `-d`, `--digits INTEGER` (N-Digit Shortcut)

Generates N-digit numbers without a manual `[min, max]` range.

* **Standard mode (`-d N`):** Sets `min_val = 10^(N-1)` and `max_val = 10^N - 1`. For example, `-d 6` generates a number from `100000` to `999999`.
* **Zero-padded mode (`-d N --pad`):** Sets `min_val = 0` and `max_val = 10^N - 1`, and prints each value as an N-character string with leading zeros. For example, `-d 6 --pad` generates strings from `"000000"` to `"999999"`, such as `"004819"`.

**Do not use `-d` output as production 2FA codes, PINs, seeds, or keys.** The default backend is a simulator PRNG. On hardware, the conditioning assumes a min-entropy that is never measured or health-tested, and output is written to stdout and to plaintext evidence. Production key generation needs a vetted entropy source, continuous health tests, and protected hardware signing keys, none of which `vqrng` provides. Use `-d` for demonstrations, research, and test data.

---

## Quick Start: Python SDK

You can also import `vqrng` directly into Python applications.

### Generation

```python
import vqrng

# Generate a pool of 10 random integers using Qiskit Aer (a PRNG simulator, for testing)
evidence = vqrng.generate(
    min_val=1,
    max_val=100,
    mode="aer",
    pool_size=10,
    chsh=True,                 # optional CHSH spot-check in the first job
    signing_key=None,          # optional hex Ed25519 private key seed
)

print(f"Generated Numbers: {[item['number'] for item in evidence['items']]}")
print(f"Canonical Pool Hash: {evidence['pool_hash']}")
```

If sampling stops before the pool fills, or the CHSH test does not finish, `generate` raises `vqrng.GenerationError`. Its `evidence` attribute holds the payload measured so far:

```python
try:
    evidence = vqrng.generate(1, 100, mode="hardware", runtime_limit=300, pool_size=50)
except vqrng.GenerationError as exc:
    evidence = exc.evidence  # status == "partial"; accepted values, job ids, and shots so far
```

`backend` accepts either an IBM QPU name (hardware mode) or a `vqrng.BaseBackend` instance to run on instead of the default. A backend can override `run_many` to put several circuits in one job.

The conditioning step is available on its own:

```python
digest = vqrng.extract_entropy("0110" * 128)  # 32-byte HMAC-SHA256, keyed with b"vqrng-v1-extractor"
```

### Offline Verification

```python
import vqrng

# Verify an evidence dictionary or JSON string
result = vqrng.verify(evidence, trusted_keys=["<PUBLIC_KEY_HEX>"])  # trusted_keys is optional

if result.is_valid:
    print("Verification passed")
else:
    print(f"Verification failed: {result.errors}")

for level in result.levels.values():
    print(level.level, level.status, level.errors)  # status is "pass", "fail", or "skipped"

print(result.level_b_assurance)  # "checksum-only", "signed-untrusted-key", "authentic", or None if Level B failed
print(result.level_c_passed, result.chsh_s_value)  # e.g. True 2.83 with chsh=True
```

---

## Verification Levels & Guarantee Hierarchy

`vqrng` structures verification into three tiers:

| Level | Name | Description | What It Shows |
| --- | --- | --- | --- |
| Level A | Reproducible Conversion | Replays each conditioned candidate through the rejection sampler. | The range conversion was computed correctly and without modulo bias. |
| Level B | Tamper-Evident Provenance | Recomputes the circuit and payload SHA-256 hashes, re-conditions the raw tape and replays the items from it, and checks any Ed25519 signature over `pool_hash`. | Provides self-consistency checks via canonical SHA-256 hashes and optional asymmetric signature verification. Only a signature from a key you already trust shows who produced the record. |
| Level C | Near-real-time Spot-Checking Audit | Computes the CHSH value *S* from Bell-test counts recorded with `--chsh`, and checks the runs' backend and timing against the pool. | *S* > 2 physical non-locality consistency check. The reported counts violate the classical bound, and they were taken on the pool's backend within 10 seconds of it. This is not device-independent certification. The pool comes from a separate circuit, and without a trusted signature nothing proves who recorded the counts. |

### Limits

* **The conditioning does not create entropy.** HMAC-SHA256 is a vetted conditioning function in NIST SP 800-90B. The 512 → 256 bit ratio assumes at least 0.5 bits of min-entropy per raw bit, and `vqrng` does not estimate or health-test that. A constant or simulated source still gives random-looking, but predictable, output. The fixed public salt makes this a deterministic conditioner, not a seeded extractor in the sense of the Leftover Hash Lemma.
* **Level C does not certify the pool.** The Bell circuits run beside the Hadamard circuit that produces the numbers. The locality and detection loopholes are open. The timing check relies on the times the backend reported.
* **Signatures authenticate, they do not attest.** A trusted signature shows that the key holder produced the record. It says nothing about whether the key holder ran the circuits honestly.

---

## Tech Stack

* **Quantum Framework:** Qiskit 1.x, `qiskit-ibm-runtime`
* **Simulation Engine:** `qiskit-aer`
* **Cryptography:** `hmac`, `hashlib` (standard library); Ed25519 verification in pure Python (RFC 8032); signing via optional `cryptography`
* **CLI & Packaging:** `argparse`, `setuptools`, `pyproject.toml`
* **Language:** Python 3.10+

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
