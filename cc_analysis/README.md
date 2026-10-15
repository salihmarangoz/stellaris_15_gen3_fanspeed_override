# OEM Control Center analysis

Interoperability findings for this project's exact Stellaris 15 Gen3. Proprietary
OEM binaries, decrypted method bodies, memory images and firmware dumps are not
redistributed. Local raw artifacts remain ignored under build/oem-analysis/.

- [NVIDIA Optimus / firmware MUX](nvidia-optimus.md): confirmed UEFI variable,
  offset, mode encoding, observation procedure, implementation limits.
- Previous GPU power/WhisperMode investigation: local
  build/oem-analysis/gpu-static-findings.md. Its protected-body limitation was
  specific to static analysis. The 2026-10-15 investigation also used a read-only
  copy of the already-running OEM service's code image.

Future Control Center interoperability findings belong in this directory. Keep
numeric facts and independently written summaries here, and raw OEM artifacts
out of Git.

- [Control Center 4.9 fan transport](control-center-4.9-fans.md): exact-image support, subsystem bit preservation and owner-authorized Windows test.
