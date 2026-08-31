import sys

def start_repl():
    from .repl.core import BioPasterREPL
    repl = BioPasterREPL()
    repl.run()
    return 0

def main():
    """CLI main entry point."""
    if len(sys.argv) == 2 and sys.argv[1] in ['--version', '-v', '-V']:
        from . import __version__
        print(f"BioPaster version {__version__}")
        return 0
    return start_repl()

if __name__ == '__main__':
    sys.exit(main())