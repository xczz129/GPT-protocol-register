# Sanitized Source Export

This archive contains the application source, tests, Vue source tree, example
configuration, and compiled web assets.

The export excludes local configuration, account records, passwords, TOTP
secrets, access and refresh tokens, mailbox state, proxy pools, CDKs, task
history, backups, logs, HAR captures, screenshots, caches, package
dependencies, Git metadata, local binaries, and generated credential exports.

To start from this archive, copy `config.example.yaml` to `config.yaml` and
replace every placeholder with local values. Runtime directories and files are
created as needed.
