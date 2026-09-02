from shared.fan_control_ipc import is_administrator, launch_component


def main() -> None:
    if is_administrator():
        launch_component("frontend")
        return

    from frontend.fan_control_gui import main as run_frontend

    run_frontend()


if __name__ == "__main__":
    main()
