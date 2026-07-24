"""Entry point for the PhidgetBridge HMI application."""

import sys

from hmi import BridgeHMI


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "."
    app = BridgeHMI(start_folder=start)
    app.mainloop()


if __name__ == "__main__":
    main()
