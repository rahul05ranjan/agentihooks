### Fixed

- The sixteen concurrent ledger task add test keeps its writers' client
  deadline valid under mutation tracing, so mutation statistics finish their
  clean baseline instead of losing a writer to a socket timeout.
