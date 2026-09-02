# Accidents

This is the factual log of important failures or harmful behavior encountered by this project or the target system. Every entry must have a timestamp, title, problem, outcomes, and solution. When the actual incident time is unknown, the entry must say so and use the earliest recorded remediation timestamp instead of inventing a time. Design risks and unconfirmed possibilities belong in `TODO.md`.

## 2026-09-02T02:58:04+03:00 - OEM CPU temperature path became untrustworthy

Actual incident time: unknown. The heading uses the timestamp of commit `cce4712`, where the remediation was first recorded in this repository.

### Problem

The target laptop's embedded-controller/OEM Control Center path stopped receiving the correct CPU temperature. The stock fan behavior could therefore make decisions from a stale or incorrect CPU reading.

### Outcomes

- The original fan curve could leave the fans too slow while the CPU was heating up.
- Continuing to use the OEM, EC, or MQTT CPU reading in Automatic mode became unsafe.
- Overheating, instability, shutdown, hardware damage, and data loss became credible failure outcomes.

### Solution

CPU temperature now comes from the Ryzen `Tctl/Tdie` SMN register `0x00059800` through PawnIO's restricted `AMDFamily17` module. GPU temperature comes independently from the NVIDIA driver. Automatic control fails closed when either reading is missing, zero, malformed, or implausible, uses the hotter valid reading, and preserves OEM Fan Boost as the immediate 100% fallback.
