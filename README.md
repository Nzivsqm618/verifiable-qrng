# Verifiable Quantum Random Number Generator (QRNG)

[![Qiskit](https://img.shields.io/badge/Qiskit-1.x-6929C4?logo=qiskit&logoColor=white)](https://qiskit.org/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An open-source, device-independent protocol for generating and physically certifying quantum entropy using Qiskit. 

Unlike classical Pseudo-Random Number Generators (PRNGs) or unverified Hardware RNGs, this implementation relies on **Bell's Theorem (CHSH Inequality)** to mathematically prove that the generated random bits originate from genuine quantum non-locality rather than physical system noise or classical predictability.

---

## Executive Summary & Goals

The primary goal of this project is to build a verifiable randomness pipeline on public cloud Quantum Processing Units (QPUs).

* **Physical Entropy Certification:** Utilize non-local quantum entanglement to prove true physical randomness.
* **Device Independence:** Verify randomness quality without assuming ideal or trusted physical quantum hardware.
* **Cloud QPU Execution:** Leverage Qiskit Runtime primitives (`SamplerV2`) for optimized execution on real hardware.

---

## Key Features & Architecture

* **CHSH Circuit Engine:** Dynamic implementation of maximally entangled Bell state measurements across non-orthogonal measurement bases ($A_0, A_1, B_0, B_1$).
* **Entanglement Verification:** Automated computation of the CHSH parameter ($S$). 
  * Classical Bound: $S \le 2$
  * Quantum Violation Certified: $S > 2$ (up to Tsirelson's bound $2\sqrt{2} \approx 2.828$)
* **Randomness Extraction:** Post-processing pipeline (Von Neumann extraction) to remove physical measurement bias and output uniformly distributed bitstreams.
* **Benchmarking Suite:** Side-by-side performance comparison across:
  1. Ideal Statevector Simulator (`qiskit-aer`)
  2. Realistic Backend Noise Models (Thermal Relaxation & Readout Errors)
  3. Live IBM Quantum Processing Units (QPUs)
* **Statistical Auditing:** Integration with statistical randomness testing frameworks (e.g., NIST SP 800-22 suite).

---

## Tech Stack

* **Quantum Framework:** Qiskit 1.x, `qiskit-ibm-runtime`
* **Simulation Engine:** `qiskit-aer`
* **Language:** Python 3.10+
* **Data Analysis & Viz:** NumPy, SciPy, Matplotlib

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
