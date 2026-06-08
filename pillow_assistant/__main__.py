"""Enable ``python -m pillow_assistant`` as an entry point."""

from pillow_assistant.app import main

if __name__ == "__main__":
    raise SystemExit(main())
