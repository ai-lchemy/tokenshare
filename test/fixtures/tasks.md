# Token Share Queue

Malformed blocks are ignored:

<!-- tokenshare-task:start -->
- State: Maybe
- Repo: https://gitlab.example/acme/bad.git
- Title: Invalid state
<!-- tokenshare-task:end -->

<!-- tokenshare-task:start -->
- State: Pending
- Repo: https://gitlab.example/acme/first.git
- Title: First pending task

Do the first thing.
<!-- tokenshare-task:end -->

<!-- tokenshare-task:start -->
- State: Done
- Repo: https://gitlab.example/acme/done.git
- Title: Completed task
<!-- tokenshare-task:end -->

<!-- tokenshare-task:start -->
- State: Pending
- Repo: https://gitlab.example/acme/last.git
- Title: Bottom pending task

Do the bottom-most pending thing.
<!-- tokenshare-task:end -->

<!-- tokenshare-task:start -->
- State Pending
- Repo: https://gitlab.example/acme/malformed.git
- Title: Missing state delimiter
<!-- tokenshare-task:end -->
