"""Entry point for packaged desktop sidecar binary."""

from nanobot.cli.commands import desktop_gateway


if __name__ == "__main__":
    desktop_gateway(host=None, port=None, verbose=False)
