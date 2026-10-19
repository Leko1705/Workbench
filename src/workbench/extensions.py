"""A2A extensions that workers and the workbench agree on (declared in the agent card)."""

# "btw": while a task runs, the caller may add a message that the agent takes up at its next
# safe point (between generation and tool steps). Not part of core A2A: the SDK would start a
# second concurrent execution for a second message to a running task, so the workbench instead
# POSTs {"task_id", "text"} to `<a2a url>/btw` with the same bearer token it uses for A2A.
BTW_EXTENSION_URI = "https://github.com/Leko1705/Workbench/ext/btw/v1"
BTW_PATH = "/btw"
