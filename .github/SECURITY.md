# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✅ |
| < 1.0   | ❌ |

## Reporting a vulnerability

**Please do not file a public GitHub issue for security
vulnerabilities.**

Email security disclosures to:

> **rollroyces@users.noreply.github.com**

You should receive an acknowledgement within **3 business days**.
We aim to triage and patch within **14 days** for critical
vulnerabilities, **30 days** for high-severity, and **90 days**
for everything else.

When reporting, please include:

* A clear description of the vulnerability and its impact.
* A minimal reproducer (snippet, command sequence, or input data).
* The version of `nl2pbip` and Python you observed the issue on.
* Whether you want public credit in the release notes.

We follow **coordinated disclosure**: we'll work with you on a
fix-and-disclosure timeline before any public announcement.

## Scope

The following are in scope:

* TMDL / PBIR parsing bugs that can corrupt or execute
  attacker-controlled content.
* Prompt-injection vectors through `nl2pbip.prompts` or any
  built-in tool description.
* Insecure deserialisation in `nl2pbip.exporter.opc` or
  `nl2pbip.exporter.pbit_builder`.
* Dependency vulnerabilities in the **runtime** tree
  (`jsonschema`, `openai`, `requests`).
* Anything in `nl2pbip.m_builder` that could let crafted
  M-expressions escape their sandbox.

Out of scope:

* The `finetune` extra (`transformers`, `trl`, `unsloth`) — those
  are maintained by their upstream authors.
* Issues in third-party LLM providers.

## Hall of fame

We credit reporters in the next release notes unless they ask
to remain anonymous.
