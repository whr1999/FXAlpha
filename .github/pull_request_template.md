## Change

Describe the user-visible and contract-level change.

## Evidence

- [ ] Relevant tests pass
- [ ] `python scripts/audit_public_repo.py` passes
- [ ] `python scripts/audit_git_history.py` passes
- [ ] `python scripts/verify_publication_topology.py` passes
- [ ] Documentation is updated
- [ ] No data, runtime state, database, artifact, log, or credential is included
- [ ] Third-party changes were made in a fork and only the submodule pin changed here

## Production impact

State whether this is source-only, requires migration/cutover, or changes a
promotion/write gate. Include rollback instructions for any runtime change.
