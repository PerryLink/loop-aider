# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.2.x   | :white_check_mark: Active support |
| 0.1.x   | :x: End of life    |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do NOT** create a public Issue.

Send an email to the project maintainer with the following information:

1. Detailed description of the vulnerability
2. Steps to reproduce
3. Affected version(s)
4. Suggested fix (if available)

We will acknowledge receipt within 48 hours and provide an initial assessment
within 7 days.

## Security Architecture

### 1. Safe-by-Default Trust Levels

loop-aider enforces four trust levels, each with escalating Gate activation:

| Mode        | Trust Level | Gates Active | Human-in-the-Loop |
|-------------|-------------|-------------|--------------------|
| safe        | L1          | G1-G5       | Always             |
| interactive | L1+         | G1-G5       | Decision points    |
| auto        | L2          | G1, G4      | None               |
| unsafe      | L3          | G4 only     | None               |

### 2. Five-Layer Gate Protocol

Every Aider invocation passes through a layered defense:

- **G1 Content Safety**: Blocks malware/backdoor/banned behavior prompts
- **G2 Plan Confirmation**: Pauses for human approval at design transitions
- **G3 Dependency Audit**: Detects unauthorized pip/npm install commands
- **G4 Catastrophic Ops**: Hard-blocks `rm -rf /`, `DROP TABLE`, `mkfs`, etc.
- **G5 File Change Guard**: Limits modified file count per cycle

### 3. Post-Call Audits

After each Aider execution, five audits verify output integrity:

| Audit | Name                 | Severity on Failure |
|-------|----------------------|---------------------|
| A1    | Exit Code            | P2                  |
| A2    | File Changes         | P2                  |
| A3    | Token Consumption    | P2                  |
| A4    | Artifact Integrity   | P1                  |
| A5    | Banned Behaviors     | P0                  |

- P0 findings trigger immediate rollback to Part 1 (re-design)
- P1 findings enter the routing decision tree
- P2 findings trigger automatic repair mode

### 4. Git Safety Net

- Every change is committed atomically with semantic commit messages
- Before each Aider run, a local backup branch is created
- Rollback is a single `git reset --hard` away
- Force push to protected branches is blocked
- All commits include the Aider phase and cycle number for traceability

### 5. Convergence Engine Safety

The convergence engine ensures the loop terminates safely:

- **max_cycles** hard limit prevents infinite loops
- **convergence_counter** requires N consecutive cycles with no new issues
- New issues immediately reset the counter
- External errors (timeout, API error) do not penalize the counter

### 6. Subprocess Sandboxing

Aider runs as a subprocess with:
- Configurable timeout (default 600s)
- Environment isolation (API keys injected per-invocation)
- Output capture for audit trail
- Exit code validation

### 7. Atomic State Persistence

- `state.json` written atomically via tmp+fsync+rename
- Pre-write backup to `state.json.bak`
- File-level locking prevents concurrent corruption
- Zombie lock auto-cleanup after 300s timeout

### 8. Sensitive Information Protection

- API keys read from environment variables only
- Never stored in `state.json` or config files
- Logging strips API key content
- Aider subprocess environment is scoped per invocation

## API Key Management Best Practices

```bash
# Recommended: environment variables
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."

# Recommended: secrets manager
# AWS Secrets Manager / GCP Secret Manager / Azure Key Vault

# NEVER: hardcode in source or config
# ANTHROPIC_API_KEY = "sk-ant-..."  # Do not do this
```

## Dependency Security

Periodically check dependencies for vulnerabilities:

```bash
# pip-audit
pip install pip-audit
pip-audit

# safety
pip install safety
safety check

# bandit (code security scan)
pip install bandit
bandit -r loop_aider/
```

## Known Security Limitations

1. **Aider Trust Boundary**: loop-aider delegates code generation to the Aider
   CLI, which may execute arbitrary commands. Use `safe` or `interactive` mode
   for untrusted projects.
2. **No Sandbox Isolation**: Aider runs in the same filesystem as loop-aider.
   The `--aider-work-dir` flag can scope it to a subdirectory.
3. **Plaintext State**: `state.json` is unencrypted. Sensitive project
   information may be readable by users with filesystem access.
4. **Network TLS**: Communication with LLM providers relies on the Aider SDK's
   TLS implementation. Ensure HTTPS endpoints are used.

## Security Checklist

Before deploying loop-aider in a production environment, confirm:

- [ ] All API keys are set via environment variables, not hardcoded
- [ ] Running mode is `safe` or `interactive` (not `auto` or `unsafe`)
- [ ] `.aider/` state directory permissions are restricted to current user
- [ ] `max_cycles` is set to a reasonable value (default 5, max 20)
- [ ] Gate G4 (catastrophic ops) is enforced in all modes
- [ ] Git pre-push hooks are configured to block force push to main
- [ ] All tests pass: `pytest tests/ -v`
- [ ] Gate thresholds (file count, dependency allowlist) have been reviewed
- [ ] The Aider version compatibility list has been checked

## Contact

For security-related inquiries, contact the project maintainer.
