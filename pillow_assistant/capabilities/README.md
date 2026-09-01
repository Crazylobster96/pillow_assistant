# Pillow capability resources

This package keeps model-facing capability data separate from execution code.

- `prompts/`: versioned Markdown templates listed in `catalog.json`.
- `tools/<name>/manifest.json`: localized descriptions, JSON schema, version, and modes.
- `skills/builtin/`: packaged `SKILL.md` workflows. User and project Skills are layered at runtime.

Security invariants remain in Python. In particular, a tool manifest cannot change
the permission declared by its implementation; the registry rejects mismatches.
Project data is supporting context, not a system-prompt override.

Prompt templates use `{{variable}}` placeholders. The registry rejects missing or
unknown variables and exposes source/version/SHA-256 metadata for audit logs.
