# Verifiable Quantum Random Number Generator (`vqrng`)

[![Qiskit](https://img.shields.io/badge/Qiskit-1.x-6929C4?logo=qiskit&logoColor=white)](https://qiskit.org/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![CLI Tool](https://img.shields.io/badge/CLI-vqrng-green.svg)](https://github.com/your-org/vqrng)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An open-source Python library and Unix CLI tool (`vqrng`) for generating unbiased random integers and cryptographically verifiable quantum entropy using Qiskit.

`vqrng` bridges the gap between quantum circuit execution and real-world application pipelines, combining **unbiased rejection sampling** with **tamper-evident JSON evidence logging** and optional **Bell's Theorem (CHSH Inequality)** device-independent certification.

---

## Executive Summary & Goals

Classical Pseudo-Random Number Generators (PRNGs) and unverified Hardware RNGs rely on opaque physical noise or deterministic seed states. `vqrng` provides a complete, dual-interface (SDK + CLI) framework for verifiable quantum randomness:

* **Unbiased Integer Conversion:** Employs strict rejection sampling (n<sub>bits</sub> = ⌈log<sub>2</sub>(range_size)⌉) to eliminate modulo bias when mapping raw quantum bits to custom integer ranges `[min, max]`.
* **Tamper-Evident Provenance:** Automatically records raw bitstrings, rejected candidate states, circuit hashes, and canonical SHA-256 pool hashes for offline auditability.
* **Dual Execution Modes:** Supports local high-speed simulation via `qiskit-aer` and physical quantum processor execution on IBM Quantum QPUs.
* **Graduated Verification Hierarchy:** Supports basic conversion audits (Level A), canonical hash/provenance validation (Level B), and physical non-locality certification via Bell/CHSH inequality tests (Level C).

---

## Key Features

* **Python SDK & Unix Pipeline CLI:** Import `vqrng` directly inside Python projects or chain `vqrng` in Unix shell pipelines. Stdout is plain numbers by default, one per line. Pass `-r` for one space-separated line, or `-j` for the canonical JSON evidence payload.
* **Strict Rejection Sampling:** Guarantees zero modulo bias across any arbitrary `[min, max]` bounds.
* **QPU Budget Control:** Enforces maximum execution runtime limits for IBM Quantum jobs, cleanly separating queue duration from active QPU compute time.
* **Offline Verification Engine:** Includes a built-in verification suite (`vqrng verify`) to audit evidence files and detect post-generation tampering.
* **CHSH Entanglement Engine:** Optional device-independent entropy verification using non-orthogonal measurement bases (A<sub>0</sub>, A<sub>1</sub>, B<sub>0</sub>, B<sub>1</sub>) to prove *S* > 2 quantum non-locality.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/your-username/vqrng.git
cd vqrng

# Install library and CLI executable in editable mode
pip install -e .
```

---

## Quick Start: CLI Usage

By default, `vqrng` writes plain random numbers to stdout (`74`, or one number per line for a pool) so the output is ready for shell scripts and pipes. Logs and status updates go to stderr. Pass `-r` / `--raw` to print a pool on one space-separated line, or `-j` / `--json` to write the full canonical JSON evidence payload instead.

Help is `--help`. `-h` selects IBM Quantum hardware, not help.

### 1. Basic Generation (Simulator Mode)

Generate a single random integer between 1 and 100 using Qiskit Aer:

```bash
vqrng -s 1 100
```

### 2. Generate a Random Pool

Generate a pool of 20 random numbers in the range [1, 1000]:

```bash
vqrng -s -p 20 1 1000
```

### 3. Run on IBM Quantum Hardware

Submit to physical IBM QPU hardware with a 300-second maximum QPU runtime budget:

```bash
export IBMQ_API_TOKEN="your_ibm_quantum_api_token"
vqrng -h -t 300 -p 10 1 100
```

### 4. JSON Evidence and Verification

Write the evidence payload, pipe it into the verifier, or audit a saved file. Verification requires the JSON payload, so generation commands that feed `vqrng verify` must include `-j`.

```bash
# Full cryptographic evidence on stdout
vqrng -s -j 1 100

# Pipeline verification
vqrng -s -j -p 50 1 1000 | vqrng verify

# File verification
vqrng verify evidence.json
```

### 5. N-Digit Tokens (OTP and PIN)

Generate an N-digit number without setting `MIN_VAL` and `MAX_VAL` by hand:

```bash
# 6-digit number in [100000, 999999]
vqrng -s -d 6

# Zero-padded 6-character token from "000000" to "999999"
vqrng -s -d 6 --pad
```

---

## CLI Flags

### `-j`, `--json` (Evidence Payload Output)

Running `vqrng` without `-j` prints only plain random numbers. Supplying `-j` or `--json` prints the full cryptographic canonical JSON evidence dictionary instead.

Use it for verification logging, cryptographic audit trails, piping into `vqrng verify`, and storing provenance records. `-j` cannot be combined with `-r`.

### `-r`, `--raw` (Single-Line Output)

Prints the pool as space-separated values on one line, for example `741829 938201`. The default remains one value per line. `-r` cannot be combined with `-j`.

```bash
vqrng -s -r -p 3 1 100
```

### `--help`

Prints the generated usage text and exits. `-h` / `--hardware` selects IBM Quantum hardware and requires `-t` / `--runtime`.

### `-d`, `--digits INTEGER` (Quantum OTP and PIN Shortcut)

Generates an N-digit quantum random number or security token without a manual `[min, max]` range.

* **Standard mode (`-d N`):** Sets `min_val = 10^(N-1)` and `max_val = 10^N - 1`. For example, `-d 6` generates a number from `100000` to `999999`.
* **Zero-padded mode (`-d N --pad`):** Sets `min_val = 0` and `max_val = 10^N - 1`, and prints each value as an N-character string with leading zeros. For example, `-d 6 --pad` generates tokens from `"000000"` to `"999999"`, such as `"004819"`.

Use it for quantum-safe 2FA OTP codes (Q-OTP), hardware wallet PINs, cryptographic seed codes, and recovery keys.

---

## Quick Start: Python SDK

You can also import `vqrng` directly into Python applications.

### Generation

```python
import vqrng

# Generate a pool of 10 random integers using Qiskit Aer
evidence = vqrng.generate(
    min_val=1,
    max_val=100,
    mode="aer",
    pool_size=10
)

print(f"Generated Numbers: {[item['number'] for item in evidence['items']]}")
print(f"Canonical Pool Hash: {evidence['pool_hash']}")
```

### Offline Verification

```python
import vqrng

# Verify previously generated evidence dictionary or JSON string
result = vqrng.verify(evidence)

if result.is_valid:
    print("✓ Verification Passed: Hash and conversion logic verified!")
else:
    print(f"✗ Verification Failed: {result.errors}")
```

---

## Verification Levels & Guarantee Hierarchy

`vqrng` structures verification into three distinct guarantee tiers:

| Level | Name | Description | What It Proves |
| --- | --- | --- | --- |
| Level A | Reproducible Conversion | Replays raw measurement bits against the rejection sampling algorithm. | Proves range conversion was computed correctly from raw bits without modulo bias. |
| Level B | Tamper-Evident Provenance | Audits canonical SHA-256 pool hashes, circuit QASM hashes, and backend IDs. | Proves evidence content has not been altered post-generation under trusted server key models. |
| Level C | Device-Independent Certification | Executes Bell/CHSH violation protocol ($S > 2$). | Mathematically proves physical non-locality and true quantum entropy independent of hardware trust. |

---

## Tech Stack

* **Quantum Framework:** Qiskit 1.x, `qiskit-ibm-runtime`
* **Simulation Engine:** `qiskit-aer`
* **CLI & Packaging:** `argparse`, `setuptools`, `pyproject.toml`
* **Language:** Python 3.10+

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
