"""Entry point: `python -m cmrelocator` or `cmrelocator`."""
from cmrelocator.tui import CMRelocatorApp


def main() -> None:
    CMRelocatorApp().run()


if __name__ == "__main__":
    main()
