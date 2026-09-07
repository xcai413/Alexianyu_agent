"""Compatibility entrypoint for the canonical runtime watchdog."""

from xianyu_agent.runtime.watchdog import (
    _run_once,
    check_and_recover,
    daemon_domain,
    end_task,
    is_service_paused,
    main,
    observe_daemon,
    sample_active_soaks,
    start_task,
)

__all__ = ["check_and_recover", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
