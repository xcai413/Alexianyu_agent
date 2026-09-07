"""Compatibility entrypoint for the canonical runtime service runner."""

from xianyu_agent.runtime.service_runner import (
    is_service_paused,
    main,
    run_runtime_daemon,
)

__all__ = ["is_service_paused", "main", "run_runtime_daemon"]


if __name__ == "__main__":
    raise SystemExit(main())
