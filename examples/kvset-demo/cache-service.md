# Cache service (synthetic demo project)

Our chat gateway keeps a page-granular LRU prefix cache for KV state.
Requests start with volatile fields (a request id), then the stable system prompt.
We want to know whether moving the stable prefix first improves prefix reuse.
