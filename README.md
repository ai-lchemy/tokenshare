# token-share

Initial project structure for the `token-share` tool.

- `tokenshare.sh` is the installable polling runner.
- `docs/configuration.md` documents configuration, task format, safety guarantees, and operator workflow.
- `test/fixtures/tasks.md` and `test/test_tokenshare.sh` cover parsing, bottom-most selection, state transitions, and malformed task handling.

Run tests with:

```bash
./test/test_tokenshare.sh
```
