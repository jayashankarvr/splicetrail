"""Command-line interface for SpliceTrail when run as a module (python -m splicetrail)."""

import sys
import argparse
from pathlib import Path


def main():
    """Main entry point for python -m splicetrail."""
    parser = argparse.ArgumentParser(
        prog="python -m splicetrail",
        description="SpliceTrail - find where image regions were copied from",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start API server
  python -m splicetrail serve --port 8000

  # Match two images (requires click)
  python -m splicetrail match source.jpg target.jpg

  # Show version
  python -m splicetrail version

  # Run tests
  python -m splicetrail test

For more options, use the splicetrail CLI tool (if click is installed):
  splicetrail --help
        """
    )

    parser.add_argument(
        "command",
        choices=["serve", "version", "test", "match"],
        help="Command to execute"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for API server (serve command)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host for API server (serve command)"
    )
    parser.add_argument(
        "images",
        nargs="*",
        help="Image files for match command"
    )

    args = parser.parse_args()

    if args.command == "version":
        from splicetrail import __version__
        print(f"SpliceTrail version {__version__}")
        return 0

    elif args.command == "serve":
        print(f"Starting SpliceTrail API server on {args.host}:{args.port}")
        print("Press Ctrl+C to stop")
        try:
            from splicetrail.api import run_server
            try:
                run_server(host=args.host, port=args.port)
            except ValueError as e:
                # a bad SPLICETRAIL_HASH_LIST
                print(f"Error: {e}")
                return 1
        except KeyboardInterrupt:
            print("\nServer stopped")
            return 0
        except ImportError as e:
            print(f"Error: {e}")
            print("Install API dependencies: pip install splicetrail[api]")
            return 1

    elif args.command == "test":
        print("Running tests...")
        import pytest
        exit_code = pytest.main(["-v", str(Path(__file__).parent.parent.parent / "tests")])
        return exit_code

    elif args.command == "match":
        if len(args.images) < 2:
            print("Error: match command requires at least 2 images")
            print("Usage: python -m splicetrail match source.jpg target.jpg")
            return 1

        try:
            from splicetrail.cli import main as cli_main
            print("Using splicetrail CLI for match command...")
            sys.argv = ["splicetrail", "match"] + args.images
            return cli_main()
        except ImportError:
            print("Error: match command requires click")
            print("Install CLI dependencies: pip install splicetrail[cli]")
            print("\nAlternatively, use the Python API:")
            print("  from splicetrail import ForensicsEngine")
            print("  engine = ForensicsEngine()")
            print("  # ... see documentation")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
