import sys

from shared.fan_control_ipc import is_administrator, launch_component


def main() -> None:
    start_frontend = "--no-frontend" not in sys.argv[1:]
    if not is_administrator():
        arguments = () if start_frontend else ("--no-frontend",)
        launch_component("backend", *arguments)
        return

    from backend.fan_control_backend import run_backend

    run_backend(start_frontend=start_frontend)


if __name__ == "__main__":
    main()
