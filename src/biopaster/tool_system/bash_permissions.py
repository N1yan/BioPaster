from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Pattern
import os
import re
import shlex
import sys

import tree_sitter_bash
from tree_sitter import Language, Node, Parser, Tree


FlagArgType = Literal["none", "number", "string", "char", "{}", "EOF"]


@dataclass(frozen=True)
class CommandConfig:
    safe_flags: dict[str, FlagArgType]
    regex: Pattern[str] | None = None
    additional_command_is_dangerous: (
        Callable[[str, list[str]], bool] | None
    ) = None
    respects_double_dash: bool = True


def validate_flag_argument(value: str, arg_type: FlagArgType) -> bool:
    """Check a flag argument against its configured type."""
    if arg_type == "none":
        return False

    if arg_type == "number":
        return re.fullmatch(r"[0-9]+", value) is not None

    if arg_type == "string":
        return True

    if arg_type == "char":
        # Match JavaScript's one UTF-16 code unit check.
        return len(value.encode("utf-16-le", errors="surrogatepass")) == 2

    if arg_type == "{}":
        return value == "{}"

    if arg_type == "EOF":
        return value == "EOF"

    return False


def _git_reflog_is_dangerous(_raw: str, args: list[str]) -> bool:
    for token in args:
        if token and not token.startswith("-"):
            return token in {"expire", "delete", "exists"}
    return False


def _git_remote_show_is_dangerous(_raw: str, args: list[str]) -> bool:
    positional = [arg for arg in args if arg != "-n"]
    return len(positional) != 1 or re.fullmatch(r"[a-zA-Z0-9_-]+", positional[0]) is None


def _git_remote_is_dangerous(_raw: str, args: list[str]) -> bool:
    return any(arg not in {"-v", "--verbose"} for arg in args)


def _git_ref_is_dangerous(args: list[str], *, branch: bool) -> bool:
    """Preserve the source's separate branch/tag positional-argument rules."""
    required = {"--contains", "--no-contains", "--points-at", "--sort"}
    optional = {"--merged", "--no-merged"} if branch else set()
    if not branch:
        required |= {"--merged", "--no-merged", "--format", "-n"}
    i = 0
    last_flag = ""
    seen_list = False
    after_separator = False
    while i < len(args):
        token = args[i]
        if not token:
            i += 1
            continue
        if token == "--" and not after_separator:
            after_separator = True
            last_flag = ""
            i += 1
            continue
        if not after_separator and token.startswith("-"):
            if token in {"-l", "--list"} or (
                not token.startswith("--")
                and len(token) > 2
                and "=" not in token
                and "l" in token[1:]
            ):
                seen_list = True
            last_flag = token.partition("=")[0]
            i += 2 if "=" not in token and token in required else 1
        else:
            if not seen_list and last_flag not in optional:
                return True
            i += 1
    return False


def _git_tag_is_dangerous(_raw: str, args: list[str]) -> bool:
    return _git_ref_is_dangerous(args, branch=False)


def _git_branch_is_dangerous(_raw: str, args: list[str]) -> bool:
    return _git_ref_is_dangerous(args, branch=True)


def _gh_is_dangerous(_raw: str, args: list[str]) -> bool:
    for token in args:
        value = token
        if token.startswith("-"):
            _, separator, value = token.partition("=")
            if not separator:
                continue
        if "://" in value or "@" in value or value.count("/") >= 2:
            return True
    return False


def _ps_is_dangerous(_raw: str, args: list[str]) -> bool:
    return any(re.fullmatch(r"[a-zA-Z]*e[a-zA-Z]*", arg) is not None for arg in args)


def _date_is_dangerous(_raw: str, args: list[str]) -> bool:
    required = {"-d", "--date", "-r", "--reference", "--iso-8601", "--rfc-3339"}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--") and "=" in token:
            i += 1
        elif token.startswith("-"):
            i += 2 if token in required else 1
        elif not token.startswith("+"):
            return True
        else:
            i += 1
    return False


def _lsof_is_dangerous(_raw: str, args: list[str]) -> bool:
    return any(arg.startswith("+m") for arg in args)


def _tput_is_dangerous(_raw: str, args: list[str]) -> bool:
    dangerous = {
        "init", "reset", "rs1", "rs2", "rs3", "is1", "is2", "is3",
        "iprog", "if", "rf", "clear", "flash", "mc0", "mc4", "mc5",
        "mc5i", "mc5p", "pfkey", "pfloc", "pfx", "pfxl", "smcup", "rmcup",
    }
    i = 0
    after_separator = False
    while i < len(args):
        token = args[i]
        if token == "--":
            after_separator = True
            i += 1
        elif not after_separator and token.startswith("-"):
            if token == "-S" or (
                not token.startswith("--") and len(token) > 2 and "S" in token
            ):
                return True
            i += 2 if token == "-T" else 1
        else:
            if token in dangerous:
                return True
            i += 1
    return False


def _pyright_is_dangerous(_raw: str, args: list[str]) -> bool:
    return any(arg in {"--watch", "-w"} for arg in args)


def _sed_expression_is_dangerous(expression: str) -> bool:
    """Port of sedValidation.ts containsDangerousOperations."""
    cmd = expression.strip()
    if not cmd:
        return False
    if re.search(r"[^\x01-\x7f]", cmd) or any(char in cmd for char in "{}\n"):
        return True
    index = cmd.find("#")
    if index != -1 and not (index > 0 and cmd[index - 1] == "s"):
        return True
    patterns = (
        r"^!", r"[/\d$]!", r"\d\s*~\s*\d|,\s*~\s*\d|\$\s*~\s*\d",
        r"^,", r",\s*[+-]", r"s\\", r"\\[|#%@]", r"\\/.*[wW]",
        r"/[^/]*\s+[wWeE]",
        r"^[wW]\s*\S+", r"^\d+\s*[wW]\s*\S+", r"^\$\s*[wW]\s*\S+",
        r"^/[^/]*/[IMim]*\s*[wW]\s*\S+", r"^\d+,\d+\s*[wW]\s*\S+",
        r"^\d+,\$\s*[wW]\s*\S+", r"^/[^/]*/[IMim]*,/[^/]*/[IMim]*\s*[wW]\s*\S+",
        r"^e", r"^\d+\s*e", r"^\$\s*e", r"^/[^/]*/[IMim]*\s*e",
        r"^\d+,\d+\s*e", r"^\d+,\$\s*e", r"^/[^/]*/[IMim]*,/[^/]*/[IMim]*\s*e",
    )
    if any(re.search(pattern, cmd, re.ASCII) for pattern in patterns):
        return True
    if cmd.startswith("s/") and not re.search(r"^s/[^/]*/[^/]*/[^/]*$", cmd):
        return True
    if re.search(r"^s.", cmd) and re.search(r"[wWeE]$", cmd):
        if not re.search(r"^s([^\\\n]).*?\1.*?\1[^wWeE]*$", cmd):
            return True
    substitution = re.search(r"s([^\\\n]).*?\1.*?\1(.*?)$", cmd)
    if substitution and re.search(r"[wWeE]", substitution[2]):
        return True
    return bool(re.search(r"y([^\\\n])", cmd) and re.search(r"[wWeE]", cmd))


def _sed_is_dangerous(raw: str, args: list[str]) -> bool:
    """Read-only sed allowlist using the caller's parsed argument snapshot.

    Unlike the TS helper, this does not parse the raw command a second time.
    The caller must reject shell operators/expansions before providing args.
    """
    match = re.match(r"^\s*sed\s+", raw)
    if not match or re.search(r"-e[wWe]|-w[eE]", raw[match.end():]):
        return True
    expressions = []
    found_e = False
    found_expression = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-e", "--expression"} and i + 1 < len(args):
            found_e = True
            expressions.append(args[i + 1])
            i += 2
            continue
        if arg.startswith(("--expression=", "-e=")):
            found_e = True
            expressions.append(arg.partition("=")[2])
        elif arg.startswith("-"):
            pass
        elif not found_e and not found_expression:
            expressions.append(arg)
            found_expression = True
        else:
            break
        i += 1

    has_files = False
    positional_count = 0
    has_e = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-e", "--expression"} and i + 1 < len(args):
            has_e = True
            i += 2
            continue
        if arg.startswith(("--expression=", "-e=")):
            has_e = True
        elif not arg.startswith("-"):
            positional_count += 1
            if has_e or positional_count > 1:
                has_files = True
                break
        i += 1

    flags = [arg for arg in args if arg.startswith("-") and arg != "--"]

    def flags_allowed(allowed: set[str]) -> bool:
        return all(
            all("-" + char in allowed for char in flag[1:])
            if not flag.startswith("--") and len(flag) > 2
            else flag in allowed
            for flag in flags
        )

    printing = (
        flags_allowed({"-n", "--quiet", "--silent", "-E", "--regexp-extended", "-r", "-z", "--zero-terminated", "--posix"})
        and any(flag in {"-n", "--quiet", "--silent"} or (not flag.startswith("--") and "n" in flag) for flag in flags)
        and bool(expressions)
        and all(re.fullmatch(r"(?:[0-9]+|[0-9]+,[0-9]+)?p", part.strip()) for expression in expressions for part in expression.split(";"))
    )
    substitution = False
    if not has_files and flags_allowed({"-E", "--regexp-extended", "-r", "--posix"}) and len(expressions) == 1:
        expression = expressions[0].strip()
        if expression.startswith("s/"):
            rest = expression[2:]
            delimiters = []
            i = 0
            while i < len(rest):
                if rest[i] == "\\":
                    i += 2
                    continue
                if rest[i] == "/":
                    delimiters.append(i)
                i += 1
            substitution = len(delimiters) == 2 and re.fullmatch(r"[gpimIM]*[1-9]?[gpimIM]*", rest[delimiters[-1] + 1:]) is not None
    if not printing and not substitution:
        return True
    return any(
        (substitution and ";" in expression) or _sed_expression_is_dangerous(expression)
        for expression in expressions
    )


# Configuration snapshot from claude-code-source:
# src/tools/BashTool/readOnlyValidation.ts and
# src/utils/shell/readOnlyCommandValidation.ts.
# These tables alone do NOT authorize commands or validate their paths.
FD_SAFE_FLAGS: dict[str, FlagArgType] = {
    "-h": "none", "--help": "none", "-V": "none", "--version": "none", "-H": "none",
    "--hidden": "none", "-I": "none", "--no-ignore": "none", "--no-ignore-vcs": "none",
    "--no-ignore-parent": "none", "-s": "none", "--case-sensitive": "none", "-i": "none",
    "--ignore-case": "none", "-g": "none", "--glob": "none", "--regex": "none", "-F": "none",
    "--fixed-strings": "none", "-a": "none", "--absolute-path": "none", "-L": "none",
    "--follow": "none", "-p": "none", "--full-path": "none", "-0": "none", "--print0": "none",
    "-d": "number", "--max-depth": "number", "--min-depth": "number", "--exact-depth": "number",
    "-t": "string", "--type": "string", "-e": "string", "--extension": "string", "-S": "string",
    "--size": "string", "--changed-within": "string", "--changed-before": "string",
    "-o": "string", "--owner": "string", "-E": "string", "--exclude": "string",
    "--ignore-file": "string", "-c": "string", "--color": "string", "-j": "number",
    "--threads": "number", "--max-buffer-time": "string", "--max-results": "number",
    "-1": "none", "-q": "none", "--quiet": "none", "--show-errors": "none",
    "--strip-cwd-prefix": "none", "--one-file-system": "none", "--prune": "none",
    "--search-path": "string", "--base-directory": "string", "--path-separator": "string",
    "--batch-size": "number", "--no-require-git": "none", "--hyperlink": "string",
    "--and": "string", "--format": "string",
}

GIT_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "git diff": CommandConfig(
        safe_flags={
            "--stat": "none", "--numstat": "none", "--shortstat": "none", "--name-only": "none",
            "--name-status": "none", "--color": "none", "--no-color": "none",
            "--dirstat": "none", "--summary": "none", "--patch-with-stat": "none",
            "--word-diff": "none", "--word-diff-regex": "string", "--color-words": "none",
            "--no-renames": "none", "--no-ext-diff": "none", "--check": "none",
            "--ws-error-highlight": "string", "--full-index": "none", "--binary": "none",
            "--abbrev": "number", "--break-rewrites": "none", "--find-renames": "none",
            "--find-copies": "none", "--find-copies-harder": "none",
            "--irreversible-delete": "none", "--diff-algorithm": "string", "--histogram": "none",
            "--patience": "none", "--minimal": "none", "--ignore-space-at-eol": "none",
            "--ignore-space-change": "none", "--ignore-all-space": "none",
            "--ignore-blank-lines": "none", "--inter-hunk-context": "number",
            "--function-context": "none", "--exit-code": "none", "--quiet": "none",
            "--cached": "none", "--staged": "none", "--pickaxe-regex": "none",
            "--pickaxe-all": "none", "--no-index": "none", "--relative": "string",
            "--diff-filter": "string", "-p": "none", "-u": "none", "-s": "none", "-M": "none",
            "-C": "none", "-B": "none", "-D": "none", "-l": "none", "-S": "string",
            "-G": "string", "-O": "string", "-R": "none",
        },
    ),
    "git log": CommandConfig(
        safe_flags={
            "--oneline": "none", "--graph": "none", "--decorate": "none",
            "--no-decorate": "none", "--date": "string", "--relative-date": "none",
            "--all": "none", "--branches": "none", "--tags": "none", "--remotes": "none",
            "--since": "string", "--after": "string", "--until": "string", "--before": "string",
            "--max-count": "number", "-n": "number", "--stat": "none", "--numstat": "none",
            "--shortstat": "none", "--name-only": "none", "--name-status": "none",
            "--color": "none", "--no-color": "none", "--patch": "none", "-p": "none",
            "--no-patch": "none", "--no-ext-diff": "none", "-s": "none", "--author": "string",
            "--committer": "string", "--grep": "string", "--abbrev-commit": "none",
            "--full-history": "none", "--dense": "none", "--sparse": "none",
            "--simplify-merges": "none", "--ancestry-path": "none", "--source": "none",
            "--first-parent": "none", "--merges": "none", "--no-merges": "none",
            "--reverse": "none", "--walk-reflogs": "none", "--skip": "number",
            "--max-age": "number", "--min-age": "number", "--no-min-parents": "none",
            "--no-max-parents": "none", "--follow": "none", "--no-walk": "none",
            "--left-right": "none", "--cherry-mark": "none", "--cherry-pick": "none",
            "--boundary": "none", "--topo-order": "none", "--date-order": "none",
            "--author-date-order": "none", "--pretty": "string", "--format": "string",
            "--diff-filter": "string", "-S": "string", "-G": "string", "--pickaxe-regex": "none",
            "--pickaxe-all": "none",
        },
    ),
    "git show": CommandConfig(
        safe_flags={
            "--oneline": "none", "--graph": "none", "--decorate": "none",
            "--no-decorate": "none", "--date": "string", "--relative-date": "none",
            "--stat": "none", "--numstat": "none", "--shortstat": "none", "--name-only": "none",
            "--name-status": "none", "--color": "none", "--no-color": "none", "--patch": "none",
            "-p": "none", "--no-patch": "none", "--no-ext-diff": "none", "-s": "none",
            "--abbrev-commit": "none", "--word-diff": "none", "--word-diff-regex": "string",
            "--color-words": "none", "--pretty": "string", "--format": "string",
            "--first-parent": "none", "--raw": "none", "--diff-filter": "string", "-m": "none",
            "--quiet": "none",
        },
    ),
    "git shortlog": CommandConfig(
        safe_flags={
            "--all": "none", "--branches": "none", "--tags": "none", "--remotes": "none",
            "--since": "string", "--after": "string", "--until": "string", "--before": "string",
            "-s": "none", "--summary": "none", "-n": "none", "--numbered": "none", "-e": "none",
            "--email": "none", "-c": "none", "--committer": "none", "--group": "string",
            "--format": "string", "--no-merges": "none", "--author": "string",
        },
    ),
    "git reflog": CommandConfig(
        safe_flags={
            "--oneline": "none", "--graph": "none", "--decorate": "none",
            "--no-decorate": "none", "--date": "string", "--relative-date": "none",
            "--all": "none", "--branches": "none", "--tags": "none", "--remotes": "none",
            "--since": "string", "--after": "string", "--until": "string", "--before": "string",
            "--max-count": "number", "-n": "number", "--author": "string",
            "--committer": "string", "--grep": "string",
        },
        additional_command_is_dangerous=_git_reflog_is_dangerous,
    ),
    "git stash list": CommandConfig(
        safe_flags={
            "--oneline": "none", "--graph": "none", "--decorate": "none",
            "--no-decorate": "none", "--date": "string", "--relative-date": "none",
            "--all": "none", "--branches": "none", "--tags": "none", "--remotes": "none",
            "--max-count": "number", "-n": "number",
        },
    ),
    "git ls-remote": CommandConfig(
        safe_flags={
            "--branches": "none", "-b": "none", "--tags": "none", "-t": "none",
            "--heads": "none", "-h": "none", "--refs": "none", "--quiet": "none", "-q": "none",
            "--exit-code": "none", "--get-url": "none", "--symref": "none", "--sort": "string",
        },
    ),
    "git status": CommandConfig(
        safe_flags={
            "--short": "none", "-s": "none", "--branch": "none", "-b": "none",
            "--porcelain": "none", "--long": "none", "--verbose": "none", "-v": "none",
            "--untracked-files": "string", "-u": "string", "--ignored": "none",
            "--ignore-submodules": "string", "--column": "none", "--no-column": "none",
            "--ahead-behind": "none", "--no-ahead-behind": "none", "--renames": "none",
            "--no-renames": "none", "--find-renames": "string", "-M": "string",
        },
    ),
    "git blame": CommandConfig(
        safe_flags={
            "--color": "none", "--no-color": "none", "-L": "string", "--porcelain": "none",
            "-p": "none", "--line-porcelain": "none", "--incremental": "none", "--root": "none",
            "--show-stats": "none", "--show-name": "none", "--show-number": "none", "-n": "none",
            "--show-email": "none", "-e": "none", "-f": "none", "--date": "string", "-w": "none",
            "--ignore-rev": "string", "--ignore-revs-file": "string", "-M": "none", "-C": "none",
            "--score-debug": "none", "--abbrev": "number", "-s": "none", "-l": "none",
            "-t": "none",
        },
    ),
    "git ls-files": CommandConfig(
        safe_flags={
            "--cached": "none", "-c": "none", "--deleted": "none", "-d": "none",
            "--modified": "none", "-m": "none", "--others": "none", "-o": "none",
            "--ignored": "none", "-i": "none", "--stage": "none", "-s": "none",
            "--killed": "none", "-k": "none", "--unmerged": "none", "-u": "none",
            "--directory": "none", "--no-empty-directory": "none", "--eol": "none",
            "--full-name": "none", "--abbrev": "number", "--debug": "none", "-z": "none",
            "-t": "none", "-v": "none", "-f": "none", "--exclude": "string", "-x": "string",
            "--exclude-from": "string", "-X": "string", "--exclude-per-directory": "string",
            "--exclude-standard": "none", "--error-unmatch": "none",
            "--recurse-submodules": "none",
        },
    ),
    "git config --get": CommandConfig(
        safe_flags={
            "--local": "none", "--global": "none", "--system": "none", "--worktree": "none",
            "--default": "string", "--type": "string", "--bool": "none", "--int": "none",
            "--bool-or-int": "none", "--path": "none", "--expiry-date": "none", "-z": "none",
            "--null": "none", "--name-only": "none", "--show-origin": "none",
            "--show-scope": "none",
        },
    ),
    "git remote show": CommandConfig(
        safe_flags={
            "-n": "none",
        },
        additional_command_is_dangerous=_git_remote_show_is_dangerous,
    ),
    "git remote": CommandConfig(
        safe_flags={
            "-v": "none", "--verbose": "none",
        },
        additional_command_is_dangerous=_git_remote_is_dangerous,
    ),
    "git merge-base": CommandConfig(
        safe_flags={
            "--is-ancestor": "none", "--fork-point": "none", "--octopus": "none",
            "--independent": "none", "--all": "none",
        },
    ),
    "git rev-parse": CommandConfig(
        safe_flags={
            "--verify": "none", "--short": "string", "--abbrev-ref": "none",
            "--symbolic": "none", "--symbolic-full-name": "none", "--show-toplevel": "none",
            "--show-cdup": "none", "--show-prefix": "none", "--git-dir": "none",
            "--git-common-dir": "none", "--absolute-git-dir": "none",
            "--show-superproject-working-tree": "none", "--is-inside-work-tree": "none",
            "--is-inside-git-dir": "none", "--is-bare-repository": "none",
            "--is-shallow-repository": "none", "--is-shallow-update": "none",
            "--path-prefix": "none",
        },
    ),
    "git rev-list": CommandConfig(
        safe_flags={
            "--all": "none", "--branches": "none", "--tags": "none", "--remotes": "none",
            "--since": "string", "--after": "string", "--until": "string", "--before": "string",
            "--max-count": "number", "-n": "number", "--author": "string",
            "--committer": "string", "--grep": "string", "--count": "none", "--reverse": "none",
            "--first-parent": "none", "--ancestry-path": "none", "--merges": "none",
            "--no-merges": "none", "--min-parents": "number", "--max-parents": "number",
            "--no-min-parents": "none", "--no-max-parents": "none", "--skip": "number",
            "--max-age": "number", "--min-age": "number", "--walk-reflogs": "none",
            "--oneline": "none", "--abbrev-commit": "none", "--pretty": "string",
            "--format": "string", "--abbrev": "number", "--full-history": "none",
            "--dense": "none", "--sparse": "none", "--source": "none", "--graph": "none",
        },
    ),
    "git describe": CommandConfig(
        safe_flags={
            "--tags": "none", "--match": "string", "--exclude": "string", "--long": "none",
            "--abbrev": "number", "--always": "none", "--contains": "none",
            "--first-match": "none", "--exact-match": "none", "--candidates": "number",
            "--dirty": "none", "--broken": "none",
        },
    ),
    "git cat-file": CommandConfig(
        safe_flags={
            "-t": "none", "-s": "none", "-p": "none", "-e": "none", "--batch-check": "none",
            "--allow-undetermined-type": "none",
        },
    ),
    "git for-each-ref": CommandConfig(
        safe_flags={
            "--format": "string", "--sort": "string", "--count": "number",
            "--contains": "string", "--no-contains": "string", "--merged": "string",
            "--no-merged": "string", "--points-at": "string",
        },
    ),
    "git grep": CommandConfig(
        safe_flags={
            "-e": "string", "-E": "none", "--extended-regexp": "none", "-G": "none",
            "--basic-regexp": "none", "-F": "none", "--fixed-strings": "none", "-P": "none",
            "--perl-regexp": "none", "-i": "none", "--ignore-case": "none", "-v": "none",
            "--invert-match": "none", "-w": "none", "--word-regexp": "none", "-n": "none",
            "--line-number": "none", "-c": "none", "--count": "none", "-l": "none",
            "--files-with-matches": "none", "-L": "none", "--files-without-match": "none",
            "-h": "none", "-H": "none", "--heading": "none", "--break": "none",
            "--full-name": "none", "--color": "none", "--no-color": "none", "-o": "none",
            "--only-matching": "none", "-A": "number", "--after-context": "number",
            "-B": "number", "--before-context": "number", "-C": "number", "--context": "number",
            "--and": "none", "--or": "none", "--not": "none", "--max-depth": "number",
            "--untracked": "none", "--no-index": "none", "--recurse-submodules": "none",
            "--cached": "none", "--threads": "number", "-q": "none", "--quiet": "none",
        },
    ),
    "git stash show": CommandConfig(
        safe_flags={
            "--stat": "none", "--numstat": "none", "--shortstat": "none", "--name-only": "none",
            "--name-status": "none", "--color": "none", "--no-color": "none", "--patch": "none",
            "-p": "none", "--no-patch": "none", "--no-ext-diff": "none", "-s": "none",
            "--word-diff": "none", "--word-diff-regex": "string", "--diff-filter": "string",
            "--abbrev": "number",
        },
    ),
    "git worktree list": CommandConfig(
        safe_flags={
            "--porcelain": "none", "-v": "none", "--verbose": "none", "--expire": "string",
        },
    ),
    "git tag": CommandConfig(
        safe_flags={
            "-l": "none", "--list": "none", "-n": "number", "--contains": "string",
            "--no-contains": "string", "--merged": "string", "--no-merged": "string",
            "--sort": "string", "--format": "string", "--points-at": "string",
            "--column": "none", "--no-column": "none", "-i": "none", "--ignore-case": "none",
        },
        additional_command_is_dangerous=_git_tag_is_dangerous,
    ),
    "git branch": CommandConfig(
        safe_flags={
            "-l": "none", "--list": "none", "-a": "none", "--all": "none", "-r": "none",
            "--remotes": "none", "-v": "none", "-vv": "none", "--verbose": "none",
            "--color": "none", "--no-color": "none", "--column": "none", "--no-column": "none",
            "--abbrev": "number", "--no-abbrev": "none", "--contains": "string",
            "--no-contains": "string", "--merged": "none", "--no-merged": "none",
            "--points-at": "string", "--sort": "string", "--show-current": "none", "-i": "none",
            "--ignore-case": "none",
        },
        additional_command_is_dangerous=_git_branch_is_dangerous,
    ),
}

GH_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "gh pr view": CommandConfig(
        safe_flags={
            "--json": "string", "--comments": "none", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh pr list": CommandConfig(
        safe_flags={
            "--state": "string", "-s": "string", "--author": "string", "--assignee": "string",
            "--label": "string", "--limit": "number", "-L": "number", "--base": "string",
            "--head": "string", "--search": "string", "--json": "string", "--draft": "none",
            "--app": "string", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh pr diff": CommandConfig(
        safe_flags={
            "--color": "string", "--name-only": "none", "--patch": "none", "--repo": "string",
            "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh pr checks": CommandConfig(
        safe_flags={
            "--watch": "none", "--required": "none", "--fail-fast": "none", "--json": "string",
            "--interval": "number", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh issue view": CommandConfig(
        safe_flags={
            "--json": "string", "--comments": "none", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh issue list": CommandConfig(
        safe_flags={
            "--state": "string", "-s": "string", "--assignee": "string", "--author": "string",
            "--label": "string", "--limit": "number", "-L": "number", "--milestone": "string",
            "--search": "string", "--json": "string", "--app": "string", "--repo": "string",
            "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh repo view": CommandConfig(
        safe_flags={
            "--json": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh run list": CommandConfig(
        safe_flags={
            "--branch": "string", "-b": "string", "--status": "string", "-s": "string",
            "--workflow": "string", "-w": "string", "--limit": "number", "-L": "number",
            "--json": "string", "--repo": "string", "-R": "string", "--event": "string",
            "-e": "string", "--user": "string", "-u": "string", "--created": "string",
            "--commit": "string", "-c": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh run view": CommandConfig(
        safe_flags={
            "--log": "none", "--log-failed": "none", "--exit-status": "none",
            "--verbose": "none", "-v": "none", "--json": "string", "--repo": "string",
            "-R": "string", "--job": "string", "-j": "string", "--attempt": "number",
            "-a": "number",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh auth status": CommandConfig(
        safe_flags={
            "--active": "none", "-a": "none", "--hostname": "string", "-h": "string",
            "--json": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh pr status": CommandConfig(
        safe_flags={
            "--conflict-status": "none", "-c": "none", "--json": "string", "--repo": "string",
            "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh issue status": CommandConfig(
        safe_flags={
            "--json": "string", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh release list": CommandConfig(
        safe_flags={
            "--exclude-drafts": "none", "--exclude-pre-releases": "none", "--json": "string",
            "--limit": "number", "-L": "number", "--order": "string", "-O": "string",
            "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh release view": CommandConfig(
        safe_flags={
            "--json": "string", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh workflow list": CommandConfig(
        safe_flags={
            "--all": "none", "-a": "none", "--json": "string", "--limit": "number",
            "-L": "number", "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh workflow view": CommandConfig(
        safe_flags={
            "--ref": "string", "-r": "string", "--yaml": "none", "-y": "none",
            "--repo": "string", "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh label list": CommandConfig(
        safe_flags={
            "--json": "string", "--limit": "number", "-L": "number", "--order": "string",
            "--search": "string", "-S": "string", "--sort": "string", "--repo": "string",
            "-R": "string",
        },
        additional_command_is_dangerous=_gh_is_dangerous,
    ),
    "gh search repos": CommandConfig(
        safe_flags={
            "--archived": "none", "--created": "string", "--followers": "string",
            "--forks": "string", "--good-first-issues": "string",
            "--help-wanted-issues": "string", "--include-forks": "string", "--json": "string",
            "--language": "string", "--license": "string", "--limit": "number", "-L": "number",
            "--match": "string", "--number-topics": "string", "--order": "string",
            "--owner": "string", "--size": "string", "--sort": "string", "--stars": "string",
            "--topic": "string", "--updated": "string", "--visibility": "string",
        },
    ),
    "gh search issues": CommandConfig(
        safe_flags={
            "--app": "string", "--assignee": "string", "--author": "string",
            "--closed": "string", "--commenter": "string", "--comments": "string",
            "--created": "string", "--include-prs": "none", "--interactions": "string",
            "--involves": "string", "--json": "string", "--label": "string",
            "--language": "string", "--limit": "number", "-L": "number", "--locked": "none",
            "--match": "string", "--mentions": "string", "--milestone": "string",
            "--no-assignee": "none", "--no-label": "none", "--no-milestone": "none",
            "--no-project": "none", "--order": "string", "--owner": "string",
            "--project": "string", "--reactions": "string", "--repo": "string", "-R": "string",
            "--sort": "string", "--state": "string", "--team-mentions": "string",
            "--updated": "string", "--visibility": "string",
        },
    ),
    "gh search prs": CommandConfig(
        safe_flags={
            "--app": "string", "--assignee": "string", "--author": "string", "--base": "string",
            "-B": "string", "--checks": "string", "--closed": "string", "--commenter": "string",
            "--comments": "string", "--created": "string", "--draft": "none", "--head": "string",
            "-H": "string", "--interactions": "string", "--involves": "string",
            "--json": "string", "--label": "string", "--language": "string", "--limit": "number",
            "-L": "number", "--locked": "none", "--match": "string", "--mentions": "string",
            "--merged": "none", "--merged-at": "string", "--milestone": "string",
            "--no-assignee": "none", "--no-label": "none", "--no-milestone": "none",
            "--no-project": "none", "--order": "string", "--owner": "string",
            "--project": "string", "--reactions": "string", "--repo": "string", "-R": "string",
            "--review": "string", "--review-requested": "string", "--reviewed-by": "string",
            "--sort": "string", "--state": "string", "--team-mentions": "string",
            "--updated": "string", "--visibility": "string",
        },
    ),
    "gh search commits": CommandConfig(
        safe_flags={
            "--author": "string", "--author-date": "string", "--author-email": "string",
            "--author-name": "string", "--committer": "string", "--committer-date": "string",
            "--committer-email": "string", "--committer-name": "string", "--hash": "string",
            "--json": "string", "--limit": "number", "-L": "number", "--merge": "none",
            "--order": "string", "--owner": "string", "--parent": "string", "--repo": "string",
            "-R": "string", "--sort": "string", "--tree": "string", "--visibility": "string",
        },
    ),
    "gh search code": CommandConfig(
        safe_flags={
            "--extension": "string", "--filename": "string", "--json": "string",
            "--language": "string", "--limit": "number", "-L": "number", "--match": "string",
            "--owner": "string", "--repo": "string", "-R": "string", "--size": "string",
        },
    ),
}

DOCKER_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "docker logs": CommandConfig(
        safe_flags={
            "--follow": "none", "-f": "none", "--tail": "string", "-n": "string",
            "--timestamps": "none", "-t": "none", "--since": "string", "--until": "string",
            "--details": "none",
        },
    ),
    "docker inspect": CommandConfig(
        safe_flags={
            "--format": "string", "-f": "string", "--type": "string", "--size": "none",
            "-s": "none",
        },
    ),
}

RIPGREP_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "rg": CommandConfig(
        safe_flags={
            "-e": "string", "--regexp": "string", "-f": "string", "-i": "none",
            "--ignore-case": "none", "-S": "none", "--smart-case": "none", "-F": "none",
            "--fixed-strings": "none", "-w": "none", "--word-regexp": "none", "-v": "none",
            "--invert-match": "none", "-c": "none", "--count": "none", "-l": "none",
            "--files-with-matches": "none", "--files-without-match": "none", "-n": "none",
            "--line-number": "none", "-o": "none", "--only-matching": "none", "-A": "number",
            "--after-context": "number", "-B": "number", "--before-context": "number",
            "-C": "number", "--context": "number", "-H": "none", "-h": "none",
            "--heading": "none", "--no-heading": "none", "-q": "none", "--quiet": "none",
            "--column": "none", "-g": "string", "--glob": "string", "-t": "string",
            "--type": "string", "-T": "string", "--type-not": "string", "--type-list": "none",
            "--hidden": "none", "--no-ignore": "none", "-u": "none", "-m": "number",
            "--max-count": "number", "-d": "number", "--max-depth": "number", "-a": "none",
            "--text": "none", "-z": "none", "-L": "none", "--follow": "none",
            "--color": "string", "--json": "none", "--stats": "none", "--help": "none",
            "--version": "none", "--debug": "none", "--": "none",
        },
    ),
}

PYRIGHT_READ_ONLY_COMMANDS: dict[str, CommandConfig] = {
    "pyright": CommandConfig(
        safe_flags={
            "--outputjson": "none", "--project": "string", "-p": "string",
            "--pythonversion": "string", "--pythonplatform": "string",
            "--typeshedpath": "string", "--venvpath": "string", "--level": "string",
            "--stats": "none", "--verbose": "none", "--version": "none",
            "--dependencies": "none", "--warnings": "none",
        },
        additional_command_is_dangerous=_pyright_is_dangerous,
        respects_double_dash=False,
    ),
}

COMMAND_ALLOWLIST: dict[str, CommandConfig] = {
    "xargs": CommandConfig(
        safe_flags={
            "-I": "{}", "-n": "number", "-P": "number", "-L": "number", "-s": "number",
            "-E": "EOF", "-0": "none", "-t": "none", "-r": "none", "-x": "none", "-d": "char",
        },
    ),
    **GIT_READ_ONLY_COMMANDS,
    "file": CommandConfig(
        safe_flags={
            "--brief": "none", "-b": "none", "--mime": "none", "-i": "none",
            "--mime-type": "none", "--mime-encoding": "none", "--apple": "none",
            "--check-encoding": "none", "-c": "none", "--exclude": "string",
            "--exclude-quiet": "string", "--print0": "none", "-0": "none", "-f": "string",
            "-F": "string", "--separator": "string", "--help": "none", "--version": "none",
            "-v": "none", "--no-dereference": "none", "-h": "none", "--dereference": "none",
            "-L": "none", "--magic-file": "string", "-m": "string", "--keep-going": "none",
            "-k": "none", "--list": "none", "-l": "none", "--no-buffer": "none", "-n": "none",
            "--preserve-date": "none", "-p": "none", "--raw": "none", "-r": "none", "-s": "none",
            "--special-files": "none", "--uncompress": "none", "-z": "none",
        },
    ),
    "sed": CommandConfig(
        safe_flags={
            "--expression": "string", "-e": "string", "--quiet": "none", "--silent": "none",
            "-n": "none", "--regexp-extended": "none", "-r": "none", "--posix": "none",
            "-E": "none", "--line-length": "number", "-l": "number", "--zero-terminated": "none",
            "-z": "none", "--separate": "none", "-s": "none", "--unbuffered": "none",
            "-u": "none", "--debug": "none", "--help": "none", "--version": "none",
        },
        additional_command_is_dangerous=_sed_is_dangerous,
    ),
    "sort": CommandConfig(
        safe_flags={
            "--ignore-leading-blanks": "none", "-b": "none", "--dictionary-order": "none",
            "-d": "none", "--ignore-case": "none", "-f": "none",
            "--general-numeric-sort": "none", "-g": "none", "--human-numeric-sort": "none",
            "-h": "none", "--ignore-nonprinting": "none", "-i": "none", "--month-sort": "none",
            "-M": "none", "--numeric-sort": "none", "-n": "none", "--random-sort": "none",
            "-R": "none", "--reverse": "none", "-r": "none", "--sort": "string",
            "--stable": "none", "-s": "none", "--unique": "none", "-u": "none",
            "--version-sort": "none", "-V": "none", "--zero-terminated": "none", "-z": "none",
            "--key": "string", "-k": "string", "--field-separator": "string", "-t": "string",
            "--check": "none", "-c": "none", "--check-char-order": "none", "-C": "none",
            "--merge": "none", "-m": "none", "--buffer-size": "string", "-S": "string",
            "--parallel": "number", "--batch-size": "number", "--help": "none",
            "--version": "none",
        },
    ),
    "man": CommandConfig(
        safe_flags={
            "-a": "none", "--all": "none", "-d": "none", "-f": "none", "--whatis": "none",
            "-h": "none", "-k": "none", "--apropos": "none", "-l": "string", "-w": "none",
            "-S": "string", "-s": "string",
        },
    ),
    "help": CommandConfig(
        safe_flags={
            "-d": "none", "-m": "none", "-s": "none",
        },
    ),
    "netstat": CommandConfig(
        safe_flags={
            "-a": "none", "-L": "none", "-l": "none", "-n": "none", "-f": "string", "-g": "none",
            "-i": "none", "-I": "string", "-s": "none", "-r": "none", "-m": "none", "-v": "none",
        },
    ),
    "ps": CommandConfig(
        safe_flags={
            "-e": "none", "-A": "none", "-a": "none", "-d": "none", "-N": "none",
            "--deselect": "none", "-f": "none", "-F": "none", "-l": "none", "-j": "none",
            "-y": "none", "-w": "none", "-ww": "none", "--width": "number", "-c": "none",
            "-H": "none", "--forest": "none", "--headers": "none", "--no-headers": "none",
            "-n": "string", "--sort": "string", "-L": "none", "-T": "none", "-m": "none",
            "-C": "string", "-G": "string", "-g": "string", "-p": "string", "--pid": "string",
            "-q": "string", "--quick-pid": "string", "-s": "string", "--sid": "string",
            "-t": "string", "--tty": "string", "-U": "string", "-u": "string",
            "--user": "string", "--help": "none", "--info": "none", "-V": "none",
            "--version": "none",
        },
        additional_command_is_dangerous=_ps_is_dangerous,
    ),
    "base64": CommandConfig(
        safe_flags={
            "-d": "none", "-D": "none", "--decode": "none", "-b": "number", "--break": "number",
            "-w": "number", "--wrap": "number", "-i": "string", "--input": "string",
            "--ignore-garbage": "none", "-h": "none", "--help": "none", "--version": "none",
        },
        respects_double_dash=False,
    ),
    "grep": CommandConfig(
        safe_flags={
            "-e": "string", "--regexp": "string", "-f": "string", "--file": "string",
            "-F": "none", "--fixed-strings": "none", "-G": "none", "--basic-regexp": "none",
            "-E": "none", "--extended-regexp": "none", "-P": "none", "--perl-regexp": "none",
            "-i": "none", "--ignore-case": "none", "--no-ignore-case": "none", "-v": "none",
            "--invert-match": "none", "-w": "none", "--word-regexp": "none", "-x": "none",
            "--line-regexp": "none", "-c": "none", "--count": "none", "--color": "string",
            "--colour": "string", "-L": "none", "--files-without-match": "none", "-l": "none",
            "--files-with-matches": "none", "-m": "number", "--max-count": "number",
            "-o": "none", "--only-matching": "none", "-q": "none", "--quiet": "none",
            "--silent": "none", "-s": "none", "--no-messages": "none", "-b": "none",
            "--byte-offset": "none", "-H": "none", "--with-filename": "none", "-h": "none",
            "--no-filename": "none", "--label": "string", "-n": "none", "--line-number": "none",
            "-T": "none", "--initial-tab": "none", "-u": "none", "--unix-byte-offsets": "none",
            "-Z": "none", "--null": "none", "-z": "none", "--null-data": "none", "-A": "number",
            "--after-context": "number", "-B": "number", "--before-context": "number",
            "-C": "number", "--context": "number", "--group-separator": "string",
            "--no-group-separator": "none", "-a": "none", "--text": "none",
            "--binary-files": "string", "-D": "string", "--devices": "string", "-d": "string",
            "--directories": "string", "--exclude": "string", "--exclude-from": "string",
            "--exclude-dir": "string", "--include": "string", "-r": "none",
            "--recursive": "none", "-R": "none", "--dereference-recursive": "none",
            "--line-buffered": "none", "-U": "none", "--binary": "none", "--help": "none",
            "-V": "none", "--version": "none",
        },
    ),
    **RIPGREP_READ_ONLY_COMMANDS,
    "sha256sum": CommandConfig(
        safe_flags={
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    ),
    "sha1sum": CommandConfig(
        safe_flags={
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    ),
    "md5sum": CommandConfig(
        safe_flags={
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    ),
    "tree": CommandConfig(
        safe_flags={
            "-a": "none", "-d": "none", "-l": "none", "-f": "none", "-x": "none", "-L": "number",
            "-P": "string", "-I": "string", "--gitignore": "none", "--gitfile": "string",
            "--ignore-case": "none", "--matchdirs": "none", "--metafirst": "none",
            "--prune": "none", "--info": "none", "--infofile": "string", "--noreport": "none",
            "--charset": "string", "--filelimit": "number", "-q": "none", "-N": "none",
            "-Q": "none", "-p": "none", "-u": "none", "-g": "none", "-s": "none", "-h": "none",
            "--si": "none", "--du": "none", "-D": "none", "--timefmt": "string", "-F": "none",
            "--inodes": "none", "--device": "none", "-v": "none", "-t": "none", "-c": "none",
            "-U": "none", "-r": "none", "--dirsfirst": "none", "--filesfirst": "none",
            "--sort": "string", "-i": "none", "-A": "none", "-S": "none", "-n": "none",
            "-C": "none", "-X": "none", "-J": "none", "-H": "string", "--nolinks": "none",
            "--hintro": "string", "--houtro": "string", "-T": "string", "--hyperlink": "none",
            "--scheme": "string", "--authority": "string", "--fromfile": "none",
            "--fromtabfile": "none", "--fflinks": "none", "--help": "none", "--version": "none",
        },
    ),
    "date": CommandConfig(
        safe_flags={
            "-d": "string", "--date": "string", "-r": "string", "--reference": "string",
            "-u": "none", "--utc": "none", "--universal": "none", "-I": "none",
            "--iso-8601": "string", "-R": "none", "--rfc-email": "none", "--rfc-3339": "string",
            "--debug": "none", "--help": "none", "--version": "none",
        },
        additional_command_is_dangerous=_date_is_dangerous,
    ),
    "hostname": CommandConfig(
        safe_flags={
            "-f": "none", "--fqdn": "none", "--long": "none", "-s": "none", "--short": "none",
            "-i": "none", "--ip-address": "none", "-I": "none", "--all-ip-addresses": "none",
            "-a": "none", "--alias": "none", "-d": "none", "--domain": "none", "-A": "none",
            "--all-fqdns": "none", "-v": "none", "--verbose": "none", "-h": "none",
            "--help": "none", "-V": "none", "--version": "none",
        },
        regex=re.compile("^hostname(?:\\s+(?:-[a-zA-Z]|--[a-zA-Z-]+))*\\s*$", re.ASCII),
    ),
    "info": CommandConfig(
        safe_flags={
            "-f": "string", "--file": "string", "-d": "string", "--directory": "string",
            "-n": "string", "--node": "string", "-a": "none", "--all": "none", "-k": "string",
            "--apropos": "string", "-w": "none", "--where": "none", "--location": "none",
            "--show-options": "none", "--vi-keys": "none", "--subnodes": "none", "-h": "none",
            "--help": "none", "--usage": "none", "--version": "none",
        },
    ),
    "lsof": CommandConfig(
        safe_flags={
            "-?": "none", "-h": "none", "-v": "none", "-a": "none", "-b": "none", "-C": "none",
            "-l": "none", "-n": "none", "-N": "none", "-O": "none", "-P": "none", "-Q": "none",
            "-R": "none", "-t": "none", "-U": "none", "-V": "none", "-X": "none", "-H": "none",
            "-E": "none", "-F": "none", "-g": "none", "-i": "none", "-K": "none", "-L": "none",
            "-o": "none", "-r": "none", "-s": "none", "-S": "none", "-T": "none", "-x": "none",
            "-A": "string", "-c": "string", "-d": "string", "-e": "string", "-k": "string",
            "-p": "string", "-u": "string",
        },
        additional_command_is_dangerous=_lsof_is_dangerous,
    ),
    "pgrep": CommandConfig(
        safe_flags={
            "-d": "string", "--delimiter": "string", "-l": "none", "--list-name": "none",
            "-a": "none", "--list-full": "none", "-v": "none", "--inverse": "none", "-w": "none",
            "--lightweight": "none", "-c": "none", "--count": "none", "-f": "none",
            "--full": "none", "-g": "string", "--pgroup": "string", "-G": "string",
            "--group": "string", "-i": "none", "--ignore-case": "none", "-n": "none",
            "--newest": "none", "-o": "none", "--oldest": "none", "-O": "string",
            "--older": "string", "-P": "string", "--parent": "string", "-s": "string",
            "--session": "string", "-t": "string", "--terminal": "string", "-u": "string",
            "--euid": "string", "-U": "string", "--uid": "string", "-x": "none",
            "--exact": "none", "-F": "string", "--pidfile": "string", "-L": "none",
            "--logpidfile": "none", "-r": "string", "--runstates": "string", "--ns": "string",
            "--nslist": "string", "--help": "none", "-V": "none", "--version": "none",
        },
    ),
    "tput": CommandConfig(
        safe_flags={
            "-T": "string", "-V": "none", "-x": "none",
        },
        additional_command_is_dangerous=_tput_is_dangerous,
    ),
    "ss": CommandConfig(
        safe_flags={
            "-h": "none", "--help": "none", "-V": "none", "--version": "none", "-n": "none",
            "--numeric": "none", "-r": "none", "--resolve": "none", "-a": "none",
            "--all": "none", "-l": "none", "--listening": "none", "-o": "none",
            "--options": "none", "-e": "none", "--extended": "none", "-m": "none",
            "--memory": "none", "-p": "none", "--processes": "none", "-i": "none",
            "--info": "none", "-s": "none", "--summary": "none", "-4": "none", "--ipv4": "none",
            "-6": "none", "--ipv6": "none", "-0": "none", "--packet": "none", "-t": "none",
            "--tcp": "none", "-M": "none", "--mptcp": "none", "-S": "none", "--sctp": "none",
            "-u": "none", "--udp": "none", "-d": "none", "--dccp": "none", "-w": "none",
            "--raw": "none", "-x": "none", "--unix": "none", "--tipc": "none", "--vsock": "none",
            "-f": "string", "--family": "string", "-A": "string", "--query": "string",
            "--socket": "string", "-Z": "none", "--context": "none", "-z": "none",
            "--contexts": "none", "-b": "none", "--bpf": "none", "-E": "none",
            "--events": "none", "-H": "none", "--no-header": "none", "-O": "none",
            "--oneline": "none", "--tipcinfo": "none", "--tos": "none", "--cgroup": "none",
            "--inet-sockopt": "none",
        },
    ),
    "fd": CommandConfig(
        safe_flags=dict(FD_SAFE_FLAGS),
    ),
    "fdfind": CommandConfig(
        safe_flags=dict(FD_SAFE_FLAGS),
    ),
    **PYRIGHT_READ_ONLY_COMMANDS,
    **DOCKER_READ_ONLY_COMMANDS,
}

# Keep upstream internal/network-only entries separate from the default map.
ANT_ONLY_COMMAND_ALLOWLIST: dict[str, CommandConfig] = {
    **GH_READ_ONLY_COMMANDS,
    "aki": CommandConfig(
        safe_flags={
            "-h": "none", "--help": "none", "-k": "none", "--keyword": "none", "-s": "none",
            "--semantic": "none", "--no-adaptive": "none", "-n": "number", "--limit": "number",
            "-o": "number", "--offset": "number", "--source": "string",
            "--exclude-source": "string", "-a": "string", "--after": "string", "-b": "string",
            "--before": "string", "--collection": "string", "--drive": "string",
            "--folder": "string", "--descendants": "none", "-m": "string", "--meta": "string",
            "-t": "string", "--threshold": "string", "--kw-weight": "string",
            "--sem-weight": "string", "-j": "none", "--json": "none", "-c": "none",
            "--chunk": "none", "--preview": "none", "-d": "none", "--full-doc": "none",
            "-v": "none", "--verbose": "none", "--stats": "none", "-S": "number",
            "--summarize": "number", "--explain": "none", "--examine": "string",
            "--url": "string", "--multi-turn": "number", "--multi-turn-model": "string",
            "--multi-turn-context": "string", "--no-rerank": "none", "--audit": "none",
            "--local": "none", "--staging": "none",
        },
    ),
}

SAFE_TARGET_COMMANDS_FOR_XARGS: tuple[str, ...] = (
    "echo", "printf", "wc", "grep", "head", "tail",
)

EXTERNAL_READONLY_COMMANDS: tuple[str, ...] = (
    "docker ps", "docker images",
)

READONLY_COMMANDS: tuple[str, ...] = (
    "docker ps", "docker images", "cal", "uptime", "cat", "head", "tail", "wc", "stat",
    "strings", "hexdump", "od", "nl", "id", "uname", "free", "df", "du", "locale", "groups",
    "nproc", "basename", "dirname", "realpath", "cut", "paste", "tr", "column", "tac", "rev",
    "fold", "expand", "unexpand", "fmt", "comm", "cmp", "numfmt", "readlink", "diff", "true",
    "false", "sleep", "which", "type", "expr", "test", "getconf", "seq", "tsort", "pr",
)

READONLY_COMMAND_REGEXES: tuple[Pattern[str], ...] = (
    *(re.compile(r"^" + command + r"(?:\s|$)[^<>()$`|{}&;\n\r]*$", re.ASCII)
      for command in READONLY_COMMANDS),
    re.compile("^echo(?:\\s+(?:'[^']*'|\"[^\"$<>\\n\\r]*\"|[^|;&`$(){}><#\\\\!\"'\\s]+))*(?:\\s+2>&1)?\\s*$", re.ASCII),
    re.compile("^claude -h$", re.ASCII),
    re.compile("^claude --help$", re.ASCII),
    re.compile("^uniq(?:\\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\\S+)?|-[fsw]\\s+\\d+))*(?:\\s|$)\\s*$", re.ASCII),
    re.compile("^pwd$", re.ASCII),
    re.compile("^whoami$", re.ASCII),
    re.compile("^node -v$", re.ASCII),
    re.compile("^node --version$", re.ASCII),
    re.compile("^python --version$", re.ASCII),
    re.compile("^python3 --version$", re.ASCII),
    re.compile("^history(?:\\s+\\d+)?\\s*$", re.ASCII),
    re.compile("^alias$", re.ASCII),
    re.compile("^arch(?:\\s+(?:--help|-h))?\\s*$", re.ASCII),
    re.compile("^ip addr$", re.ASCII),
    re.compile("^ifconfig(?:\\s+[a-zA-Z][a-zA-Z0-9_-]*)?\\s*$", re.ASCII),
    re.compile("^jq(?!\\s+.*(?:-f\\b|--from-file|--rawfile|--slurpfile|--run-tests|-L\\b|--library-path|\\benv\\b|\\$ENV\\b))(?:\\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\\S+)?))*(?:\\s+'[^'`]*'|\\s+\"[^\"`]*\"|\\s+[^-\\s'\"][^\\s]*)+\\s*$", re.ASCII),
    re.compile("^cd(?:\\s+(?:'[^']*'|\"[^\"]*\"|[^\\s;|&`$(){}><#\\\\]+))?$", re.ASCII),
    re.compile("^ls(?:\\s+[^<>()$`|{}&;\\n\\r]*)?$", re.ASCII),
    re.compile("^find(?:\\s+(?:\\\\[()]|(?!-delete\\b|-exec\\b|-execdir\\b|-ok\\b|-okdir\\b|-fprint0?\\b|-fls\\b|-fprintf\\b)[^<>()$`|{}&;\\n\\r\\s]|\\s)+)?$", re.ASCII),
)

def get_command_allowlist(
    *,
    platform: str | None = None,
    user_type: str | None = None,
) -> dict[str, CommandConfig]:
    """Select upstream configuration groups; this grants no permission."""
    if platform is None:
        platform = "windows" if sys.platform == "win32" else sys.platform
    if user_type is None:
        user_type = os.environ.get("USER_TYPE", "")
    allowlist = dict(COMMAND_ALLOWLIST)
    if platform == "windows":
        allowlist.pop("xargs", None)
    if user_type == "ant":
        allowlist.update(ANT_ONLY_COMMAND_ALLOWLIST)
    return allowlist


FLAG_PATTERN = re.compile(r"^-[a-zA-Z0-9_-]")


def validate_flags(
    tokens: list[str],
    start_index: int,
    config: CommandConfig,
    *,
    command_name: str = "",
    xargs_target_commands: list[str] | None = None,
) -> bool:
    """Validate flags and their values against a command configuration."""
    i = start_index

    while i < len(tokens):
        token = tokens[i]

        if not token:
            i += 1
            continue

        # xargs has its own arguments followed by another command.
        if (
            xargs_target_commands is not None
            and command_name == "xargs"
            and (not token.startswith("-") or token == "--")
        ):
            if token == "--" and i + 1 < len(tokens):
                i += 1
                token = tokens[i]

            if token in xargs_target_commands:
                break

            return False

        if token == "--":
            if config.respects_double_dash:
                break

            i += 1
            continue

        if not (
            token.startswith("-")
            and len(token) > 1
            and FLAG_PATTERN.match(token)
        ):
            # Positional arguments are checked separately.
            i += 1
            continue

        flag, separator, inline_value = token.partition("=")
        has_equals = bool(separator)
        arg_type = config.safe_flags.get(flag)

        if arg_type is None:
            # Git supports numeric shorthand such as -10.
            if command_name == "git" and re.fullmatch(r"-[0-9]+", flag):
                i += 1
                continue

            # grep/rg support attached numeric values such as -A20.
            if (
                command_name in {"grep", "rg"}
                and not flag.startswith("--")
                and len(flag) > 2
            ):
                short_flag = flag[:2]
                attached_value = flag[2:]
                attached_type = config.safe_flags.get(short_flag)

                if (
                    attached_type in {"number", "string"}
                    and re.fullmatch(r"[0-9]+", attached_value)
                    and validate_flag_argument(attached_value, attached_type)
                ):
                    i += 1
                    continue

            # Combined short flags may only contain no-argument options.
            if not flag.startswith("--") and len(flag) > 2:
                if any(
                    config.safe_flags.get("-" + char) != "none"
                    for char in flag[1:]
                ):
                    return False

                i += 1
                continue

            return False

        if arg_type == "none":
            if has_equals:
                return False

            i += 1
            continue

        if has_equals:
            value = inline_value
            i += 1
        else:
            if i + 1 >= len(tokens):
                return False

            value = tokens[i + 1]

            if (
                value.startswith("-")
                and len(value) > 1
                and FLAG_PATTERN.match(value)
            ):
                return False

            i += 2

        if arg_type == "string" and value.startswith("-"):
            reverse_git_sort = (
                flag == "--sort"
                and command_name == "git"
                and re.match(r"^-[a-zA-Z]", value) is not None
            )
            if not reverse_git_sort:
                return False

        if not validate_flag_argument(value, arg_type):
            return False

    return True


def validate_command_config(
    raw_command: str,
    tokens: list[str],
) -> bool:
    """Check parsed command tokens against the upstream configuration.

    The caller must validate shell structure and paths separately.
    This function does not grant execution permission.
    """
    if not tokens:
        return False

    for pattern, config in get_command_allowlist().items():
        prefix = pattern.split()

        if tokens[:len(prefix)] != prefix:
            continue

        args = tokens[len(prefix):]

        # Preserve the upstream git ls-remote destination checks.
        if prefix == ["git", "ls-remote"]:
            for arg in args:
                if not arg.startswith("-") and any(
                    marker in arg for marker in ("://", "@", ":", "$")
                ):
                    return False

        # Reject values whose runtime expansion cannot be established.
        for arg in args:
            if "$" in arg:
                return False
            if "{" in arg and ("," in arg or ".." in arg):
                return False

        if not validate_flags(
            tokens,
            start_index=len(prefix),
            config=config,
            command_name=tokens[0],
            xargs_target_commands=(
                list(SAFE_TARGET_COMMANDS_FOR_XARGS)
                if tokens[0] == "xargs"
                else None
            ),
        ):
            return False

        if config.regex is not None:
            if config.regex.search(raw_command) is None:
                return False
        else:
            if "`" in raw_command:
                return False
            if tokens[0] in {"grep", "rg"} and any(
                char in raw_command for char in "\n\r"
            ):
                return False

        check_dangerous = config.additional_command_is_dangerous
        if check_dangerous is not None:
            if check_dangerous(raw_command, args):
                return False

        return True

    return False


def contains_unquoted_expansion(command: str) -> bool:
    """Detect globs and variable expansions using upstream quote rules."""
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue

        # Backslashes are literal inside single quotes.
        if char == "\\" and not in_single_quote:
            escaped = True
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if in_single_quote:
            continue

        # Variables expand outside single quotes, including double quotes.
        if char == "$" and index + 1 < len(command):
            next_char = command[index + 1]
            if re.fullmatch(r"[A-Za-z_@*#?!$0-9-]", next_char):
                return True

        if in_double_quote:
            continue

        if char in "?*[]":
            return True

    return False


def is_command_readonly(
    command: str,
    tokens: list[str],
) -> bool:
    """Classify one command after shell-structure validation.

    tokens must come from parsing this same command.
    Path permissions must still be checked separately.
    """
    if not tokens:
        return False

    test_command = command.strip()

    # Match the upstream handling of stderr-to-stdout redirection.
    if test_command.endswith(" 2>&1"):
        test_command = test_command[:-5].strip()

    if contains_unquoted_expansion(test_command):
        return False

    if validate_command_config(test_command, tokens):
        return True

    for pattern in READONLY_COMMAND_REGEXES:
        if pattern.search(test_command) is None:
            continue

        # Preserve the upstream Git configuration-injection checks.
        if "git" in test_command:
            for dangerous_option in (
                r"\s-c[\s=]",
                r"\s--exec-path[\s=]",
                r"\s--config-env[\s=]",
            ):
                if re.search(dangerous_option, test_command):
                    return False

        return True

    return False


def parse_bash_command(command: str) -> Tree | None:
    """Parse Bash syntax without executing the command."""
    if not command.strip():
        return None

    parser = Parser(Language(tree_sitter_bash.language()))
    tree = parser.parse(command.encode("utf-8"))

    if tree.root_node.has_error:
        return None

    return tree

def literal_word_value(node: Node) -> str | None:
    """Decode a literal Bash word without executing expansions."""
    allowed_nodes = {
        "word",
        "number",
        "raw_string",
        "string",
        "string_content",
        "concatenation",
    }

    pending = [node]
    while pending:
        current = pending.pop()
        if current.type not in allowed_nodes:
            return None
        pending.extend(current.named_children)

    raw = node.text.decode("utf-8")
    if contains_unquoted_expansion(raw):
        return None

    result = []
    quote = None
    i = 0

    while i < len(raw):
        char = raw[i]

        if quote == "'":
            if char == "'":
                quote = None
            else:
                result.append(char)
            i += 1
            continue

        if char == "\\":
            if i + 1 >= len(raw):
                return None

            next_char = raw[i + 1]

            # Inside double quotes, only these characters are escaped.
            if quote == '"' and next_char not in '$`"\\\n':
                result.append("\\")
                i += 1
                continue

            if next_char != "\n":
                result.append(next_char)
            i += 2
            continue

        if char == '"' or (char == "'" and quote is None):
            quote = None if quote == char else char
            i += 1
            continue

        if quote is None:
            if char == "~" and (i == 0 or raw[i - 1] in "=:"):
                return None
            if char == "{" and (
                "," in raw[i:] or ".." in raw[i:]
            ):
                return None

        result.append(char)
        i += 1

    if quote is not None:
        return None

    return "".join(result)


def extract_simple_command(command: str) -> list[str] | None:
    """Compatibility entry point for one command without redirects."""
    try:
        commands = _parse_shell_commands(command)
    except (ValueError, RecursionError):
        return None
    if len(commands) != 1 or commands[0].redirects:
        return None
    return list(commands[0].argv)


def filter_out_flags(args: list[str]) -> list[str]:
    """Collect positional arguments, respecting the -- separator."""
    result = []
    after_separator = False
    for arg in args:
        if after_separator:
            result.append(arg)
        elif arg == "--":
            after_separator = True
        elif not arg.startswith("-"):
            result.append(arg)
    return result


def _pattern_paths(
    args: list[str], flags_with_args: set[str], defaults: tuple[str, ...] = (),
) -> list[str]:
    paths = []
    found_pattern = False
    after_separator = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not after_separator and arg == "--":
            after_separator = True
            continue
        if not after_separator and arg.startswith("-"):
            flag, separator, _ = arg.partition("=")
            if flag in {"-e", "--regexp", "-f", "--file"}:
                found_pattern = True
            if flag in flags_with_args and not separator:
                i += 1
            continue
        if not found_pattern:
            found_pattern = True
        else:
            paths.append(arg)
    return paths or list(defaults)


def _find_paths(args: list[str]) -> list[str]:
    path_flags = {
        "-newer", "-anewer", "-cnewer", "-mnewer", "-samefile", "-path",
        "-wholename", "-ilname", "-lname", "-ipath", "-iwholename",
    }
    paths = []
    found_predicate = False
    after_separator = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg:
            continue
        if after_separator:
            paths.append(arg)
        elif arg == "--":
            after_separator = True
        elif arg.startswith("-"):
            if arg in {"-H", "-L", "-P"}:
                continue
            found_predicate = True
            if (arg in path_flags or re.fullmatch(r"-newer[acmBt][acmtB]", arg)) and i < len(args):
                paths.append(args[i])
                i += 1
        elif not found_predicate:
            paths.append(arg)
    return paths or ["."]


def _grep_paths(args: list[str]) -> list[str]:
    flags = {flag for flag, kind in COMMAND_ALLOWLIST["grep"].safe_flags.items() if kind != "none"}
    flags |= {"--include-dir"}
    paths = _pattern_paths(args, flags)
    if not paths and any(arg in {"-r", "-R", "--recursive"} for arg in args):
        return ["."]
    return paths


def _rg_paths(args: list[str]) -> list[str]:
    flags = {flag for flag, kind in COMMAND_ALLOWLIST["rg"].safe_flags.items() if kind != "none"}
    flags |= {"--file", "-r", "--replace"}
    return _pattern_paths(args, flags, (".",))


def _sed_paths(args: list[str]) -> list[str]:
    paths = []
    found_script = False
    after_separator = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg:
            continue
        if not after_separator and arg == "--":
            after_separator = True
            continue
        if not after_separator and arg.startswith("-"):
            flag, separator, value = arg.partition("=")
            if flag in {"-f", "--file", "-e", "--expression"}:
                if not separator and i < len(args):
                    value = args[i]
                    i += 1
                if flag in {"-f", "--file"} and value:
                    paths.append(value)
                found_script = True
            elif "e" in arg or "f" in arg:
                found_script = True
            continue
        if not found_script:
            found_script = True
        else:
            paths.append(arg)
    return paths


def _jq_paths(args: list[str]) -> list[str]:
    # Unlike a generic flag filter, --arg/--argjson consume TWO values.
    arity = {"--arg": 2, "--argjson": 2, "--slurpfile": 2, "--rawfile": 2,
             "-f": 1, "--from-file": 1, "-L": 1, "--library-path": 1, "--indent": 1}
    paths = []
    found_filter = False
    after_separator = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not after_separator and arg == "--":
            after_separator = True
            continue
        if not after_separator and arg.startswith("-"):
            flag, separator, _ = arg.partition("=")
            if flag in {"-f", "--from-file"}:
                found_filter = True
            i += max(0, arity.get(flag, 0) - int(bool(separator)))
            continue
        if not found_filter:
            found_filter = True
        else:
            paths.append(arg)
    return paths


def _git_paths(args: list[str]) -> list[str]:
    if args and args[0] == "diff" and "--no-index" in args:
        return _configured_positionals("git diff", args[1:])[:2]
    return []


def _tr_paths(args: list[str]) -> list[str]:
    delete = any(arg == "--delete" or (arg.startswith("-") and "d" in arg) for arg in args)
    return filter_out_flags(args)[1 if delete else 2:]


# Source: BashTool/pathValidation.ts. Pattern commands use flag arities from
# the migrated configuration as well, so option values cannot become patterns.
PATH_EXTRACTORS: dict[str, Callable[[list[str]], list[str]]] = {
    "cd": lambda args: [str(Path.home())] if not args else [" ".join(args)],
    "ls": lambda args: filter_out_flags(args) or ["."],
    "find": _find_paths,
    **dict.fromkeys((
        "mkdir", "touch", "rm", "rmdir", "mv", "cp", "cat", "head", "tail",
        "sort", "uniq", "wc", "cut", "paste", "column", "file", "stat", "diff",
        "awk", "strings", "hexdump", "od", "base64", "nl", "sha256sum", "sha1sum", "md5sum",
    ), filter_out_flags),
    "tr": _tr_paths, "grep": _grep_paths, "rg": _rg_paths,
    "sed": _sed_paths, "git": _git_paths, "jq": _jq_paths,
}

COMMAND_OPERATION_TYPE = {
    name: "Edit" if name in {"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"} else "Read"
    for name in PATH_EXTRACTORS
}

# Supplemental explicit input options: the source's positional extractors alone
# miss --file=PATH forms. These are reads even for commands with other effects.
_PATH_OPTIONS = {
    "file": {"-f", "--files-from", "-m", "--magic-file"},
    "grep": {"-f", "--file", "--exclude-from"},
    "rg": {"-f", "--file", "--ignore-file"},
    "base64": {"-i", "--input"},
    "git": {"-O", "--ignore-revs-file", "--exclude-from", "-X"},
    "tree": {"--gitfile", "--infofile", "--hintro", "--houtro"},
    "fd": {"--ignore-file", "--search-path", "--base-directory"},
    "fdfind": {"--ignore-file", "--search-path", "--base-directory"},
    "date": {"-r", "--reference"},
    "man": {"-l"}, "info": {"-f", "--file", "-d", "--directory"},
    "ps": {"-n"}, "pgrep": {"-F", "--pidfile"}, "lsof": {"-A", "-k"},
    "pyright": {"-p", "--project", "--typeshedpath", "--venvpath"},
    "jq": {"-f", "--from-file", "-L", "--library-path"},
    "du": {"--files0-from", "--exclude-from", "-X"},
    "wc": {"--files0-from"},
    "hexdump": {"-f", "--format-file"},
    "diff": {"--from-file", "--to-file"},
    "realpath": {"--relative-to", "--relative-base"},
}

_OUTPUT_PATH_OPTIONS = {
    "sort": {"-o", "--output"},
    "tree": {"-o", "--output"},
    "info": {"-o", "--output", "--dribble"},
    "base64": {"-o", "--output"},
    "git": {"--output"},
    "find": {"-fprint", "-fprint0", "-fprintf", "-fls"},
}


def _option_values(args: list[str], flags: set[str]) -> list[str]:
    values = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        flag, separator, value = arg.partition("=")
        if flag in flags:
            if separator:
                values.append(value)
            elif i < len(args):
                values.append(args[i])
                i += 1
        elif not arg.startswith("--"):
            # GNU short options can take attached values: -f/path/to/file.
            for short in flags:
                if len(short) == 2 and arg.startswith(short) and len(arg) > 2:
                    values.append(arg[2:])
                    break
    return values


def _configured_positionals(name: str, args: list[str]) -> list[str]:
    """Remove configured option values as well as the option tokens."""
    config = COMMAND_ALLOWLIST.get(name)
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            result.extend(args[i:])
            break
        if arg.startswith("-") and arg != "-":
            flag, separator, _ = arg.partition("=")
            if config and config.safe_flags.get(flag, "none") != "none" and not separator:
                i += 1
        else:
            result.append(arg)
    return result


@dataclass(frozen=True)
class _ShellCommand:
    text: str
    argv: tuple[str, ...]
    redirects: tuple[tuple[str, str, str | None], ...] = ()


@dataclass(frozen=True)
class BashAnalysis:
    readonly: bool
    paths: tuple[tuple[str, str], ...] = ()
    commands: tuple[str, ...] = ()
    reason: str = ""


def _parse_shell_commands(command: str) -> tuple[_ShellCommand, ...]:
    """Walk an explicit AST allowlist; unsupported structure requires review."""
    if "\\\n" in command or re.search(
        r"[\x00-\x08\x0b-\x1f\x7f\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]",
        command,
    ):
        raise ValueError("Line continuations or ambiguous control/spacing characters require review.")
    tree = parse_bash_command(command)
    if tree is None:
        raise ValueError("Shell syntax could not be parsed.")
    commands = []

    def redirect(node: Node) -> tuple[str, str, str | None]:
        destination = node.child_by_field_name("destination")
        descriptor = node.child_by_field_name("descriptor")
        operators = [child.type for child in node.children if not child.is_named]
        if len(operators) != 1 or operators[0] not in {"<", ">", ">>", ">|", ">&", "<&", "&>", "&>>"}:
            raise ValueError("Unsupported redirection requires review.")
        value = literal_word_value(destination) if destination is not None else None
        if value is None:
            raise ValueError("Dynamic redirection target requires review.")
        return operators[0], value, descriptor.text.decode() if descriptor else None

    def walk(node: Node, redirects: tuple = (), source_text: str | None = None) -> None:
        if node.type in {"program", "list", "pipeline"}:
            executable = [child for child in node.named_children if child.type != "comment"]
            for child in node.children:
                if child.is_named:
                    last = bool(executable) and child == executable[-1]
                    text = None
                    if last and source_text is not None:
                        body_text = node.text.decode("utf-8")
                        if not source_text.startswith(body_text):
                            raise ValueError("Ambiguous compound redirection requires review.")
                        text = child.text.decode("utf-8") + source_text[len(body_text):]
                    walk(child, redirects if last else (), text)
                elif child.type not in {"&&", "||", "|", "|&", ";", "\n"}:
                    raise ValueError("Background execution or unsupported shell operator requires review.")
            return
        if node.type == "comment":
            return
        if node.type == "redirected_statement":
            body = node.child_by_field_name("body")
            if body is None or body.type not in {"command", "pipeline"}:
                raise ValueError("Redirected compound statement requires review.")
            extra = []
            for child in node.named_children:
                if child == body:
                    continue
                if child.type != "file_redirect":
                    raise ValueError("Here-documents and here-strings require review.")
                extra.append(redirect(child))
            walk(body, redirects + tuple(extra), node.text.decode("utf-8"))
            return
        if node.type != "command":
            raise ValueError(f"Shell structure {node.type!r} requires review.")
        argv = []
        collected = list(redirects)
        for child in node.named_children:
            if child.type == "file_redirect":
                collected.append(redirect(child))
                continue
            if child.type == "command_name":
                if len(child.named_children) != 1:
                    raise ValueError("Dynamic command name requires review.")
                child = child.named_children[0]
            value = literal_word_value(child)
            if value is None:
                raise ValueError("Assignments, substitutions or dynamic arguments require review.")
            argv.append(value)
        if not argv or not argv[0]:
            raise ValueError("Missing command name.")
        commands.append(_ShellCommand(source_text or node.text.decode("utf-8"), tuple(argv), tuple(collected)))

    walk(tree.root_node)
    if not commands:
        raise ValueError("No executable command found.")
    return tuple(commands)


def _bare_git_directory(cwd: Path) -> bool:
    git = cwd / ".git"
    if git.is_file() or (git.is_dir() and (git / "HEAD").is_file()):
        return False
    return (cwd / "HEAD").is_file() or (cwd / "objects").is_dir() or (cwd / "refs").is_dir()


def _strip_path_wrappers(argv: list[str]) -> list[str]:
    """Port the source's wrapper traversal for path/rule inspection only.

    Wrapper removal does not make a command eligible for automatic approval.
    Unknown wrapper options leave argv unchanged rather than hiding a command.
    """
    args = argv
    while args:
        name = args[0]
        if name in {"time", "nohup"}:
            args = args[2:] if args[1:2] == ["--"] else args[1:]
        elif name == "nice":
            if args[1:2] == ["-n"] and len(args) > 2 and re.fullmatch(r"-?[0-9]+", args[2]):
                i = 3
            elif len(args) > 1 and re.fullmatch(r"-[0-9]+", args[1]):
                i = 2
            else:
                i = 1
            args = args[i + 1:] if args[i:i + 1] == ["--"] else args[i:]
        elif name == "timeout":
            i = 1
            while i < len(args):
                arg = args[i]
                if arg in {"--foreground", "--preserve-status", "--verbose", "-v"}:
                    i += 1
                elif re.fullmatch(r"--(?:kill-after|signal)=[A-Za-z0-9_.+-]+|-[ks][A-Za-z0-9_.+-]+", arg):
                    i += 1
                elif arg in {"--kill-after", "--signal", "-k", "-s"} and i + 1 < len(args) and re.fullmatch(r"[A-Za-z0-9_.+-]+", args[i + 1]):
                    i += 2
                elif arg == "--":
                    i += 1
                    break
                elif arg.startswith("-"):
                    return args
                else:
                    break
            if i >= len(args) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[smhd]?", args[i]) is None:
                return args
            args = args[i + 1:]
        elif name in {"stdbuf", "env"}:
            i = 1
            while i < len(args):
                arg = args[i]
                if name == "env":
                    if ("=" in arg and not arg.startswith("-")) or arg in {"-i", "-0", "-v"}:
                        i += 1
                    elif arg == "-u" and i + 1 < len(args) and args[i + 1]:
                        i += 2
                    elif arg.startswith("-"):
                        return args
                    else:
                        break
                elif re.fullmatch(r"-[ioe]", arg) and i + 1 < len(args) and args[i + 1]:
                    i += 2
                elif re.match(r"-[ioe].|--(?:input|output|error)=", arg):
                    i += 1
                elif arg.startswith("-"):
                    return args
                else:
                    break
            if i >= len(args) or (name == "stdbuf" and i == 1):
                return args
            args = args[i:]
        else:
            return args
    return args


def _xargs_has_dynamic_paths(args: list[str]) -> bool:
    """echo/printf only display stdin; other targets may treat it as filenames."""
    flags = COMMAND_ALLOWLIST["xargs"].safe_flags
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            return i >= len(args) or args[i] not in {"echo", "printf"}
        if not arg.startswith("-"):
            return arg not in {"echo", "printf"}
        flag, separator, _ = arg.partition("=")
        if flags.get(flag, "none") != "none" and not separator:
            i += 1
    return False  # xargs defaults to echo.


def _command_paths(argv: list[str], readonly: bool) -> tuple[list[tuple[str, str]], bool]:
    """Return explicit file accesses and whether path extraction is complete."""
    name, *args = argv
    operation = COMMAND_OPERATION_TYPE.get(name, "Read")
    if name == "sed" and readonly:
        operation = "Read"
    extractor = PATH_EXTRACTORS.get(name)
    if extractor is filter_out_flags and name in COMMAND_ALLOWLIST:
        paths = _configured_positionals(name, args)
    elif extractor is not None:
        paths = extractor(args)
    elif name in {"fd", "fdfind"}:
        paths = _configured_positionals(name, args)[1:] or ["."]
    elif name == "tree":
        paths = _configured_positionals(name, args) or ["."]
    elif name in {"date", "ps", "pgrep", "info", "man"}:
        paths = []
        if name in {"info", "man"}:
            paths = [arg for arg in _configured_positionals(name, args) if "/" in arg]
    elif name in {"lsof", "pyright"}:
        paths = _configured_positionals(name, args)
    elif name in {"realpath", "readlink", "du", "df", "comm", "cmp", "fold", "expand", "unexpand", "fmt", "pr", "tac", "rev", "tsort"}:
        paths = filter_out_flags(args)
    elif name == "getconf":
        positional = filter_out_flags(args)
        paths = positional if "-a" in args or "--all" in args else positional[1:]
    else:
        paths = []
    result = [(operation, path) for path in paths]
    result.extend(("Read", path) for path in _option_values(args, _PATH_OPTIONS.get(name, set())))
    result.extend(("Edit", path) for path in _option_values(args, _OUTPUT_PATH_OPTIONS.get(name, set())))
    if name == "tee":
        result.extend(("Edit", path) for path in filter_out_flags(args))
    if name == "file":
        for value in _option_values(args, {"-m", "--magic-file"}):
            result.extend(("Read", path) for path in value.split(os.pathsep) if path)

    # Commands that derive more filenames from file/stdin contents need review.
    options = args[:args.index("--")] if "--" in args else args
    indirect = (
        (name == "file" and any(arg == "-f" or arg.startswith(("-f=", "--files-from")) for arg in options))
        or (name in {"sha256sum", "sha1sum", "md5sum"} and any(arg == "--check" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg) for arg in options))
        or (name == "find" and "-files0-from" in options)
        or (name in {"du", "wc"} and any(arg.partition("=")[0] == "--files0-from" for arg in options))
        or (name in {"fd", "fdfind"} and any(arg.partition("=")[0] == "--base-directory" for arg in options))
        or (name == "tree" and any(arg in {"--fromfile", "--fromtabfile"} for arg in options))
        or (name == "xargs" and _xargs_has_dynamic_paths(args))
    )
    return result, not indirect


def analyze_bash_command(command: str, cwd: Path) -> BashAnalysis:
    """One analysis snapshot for command classification and explicit paths.

    This is not a sandbox: imported scripts, runtime-generated paths and shell
    startup configuration are not confined by static permission checks.
    """
    try:
        parsed = _parse_shell_commands(command)
    except (ValueError, RecursionError) as exc:
        return BashAnalysis(False, reason=str(exc))
    readonly = True
    reasons = []
    accesses = []
    command_texts = []
    compound_cd = len(parsed) > 1 and any(part.argv[0] == "cd" for part in parsed)
    for part in parsed:
        argv = list(part.argv)
        canonical = shlex.join(argv)
        command_texts.extend((part.text, canonical))
        safe = is_command_readonly(canonical, argv)
        path_argv = _strip_path_wrappers(argv)
        if path_argv and path_argv != argv:
            command_texts.append(shlex.join(path_argv))
        path_safe = is_command_readonly(shlex.join(path_argv), path_argv) if path_argv else False
        paths, complete = _command_paths(path_argv or argv, path_safe)
        if not safe:
            reasons.append(f"{argv[0]} did not pass the read-only rules.")
        if not complete:
            reasons.append(f"{argv[0]} may derive paths that are not explicit in the command.")
        readonly = readonly and safe and complete
        if argv[0] == "cd" and (argv[1:] == ["-"] or any(arg.startswith("-") for arg in argv[1:])):
            readonly = False
            reasons.append("The cd destination cannot be established statically.")
        if argv[0] == "git":
            try:
                if _bare_git_directory(cwd):
                    readonly = False
                    reasons.append("Git in a directory with bare-repository indicators requires review.")
            except OSError:
                readonly = False
                reasons.append("Git directory checks could not be completed.")
        for operator, target, descriptor in part.redirects:
            if operator == ">&" and descriptor == "2" and target == "1":
                continue
            if operator == "<":
                paths.append(("Read", target))
            elif operator in {">", ">>", ">|", "&>", "&>>"} or (operator == ">&" and not target.isdecimal()):
                if target == "/dev/null" and Path(target).is_char_device():
                    paths.append(("Edit", target))
                    continue
                paths.append(("Edit", target))
                readonly = False
                reasons.append("Output redirection writes a file.")
            else:
                readonly = False
                reasons.append("File-descriptor redirection requires review.")
        # Do not report relative paths against the original cwd after a cd.
        if compound_cd:
            paths = [(op, path) for op, path in paths if Path(path).is_absolute()]
        accesses.extend(paths)
    if compound_cd:
        readonly = False
        reasons.append("A compound command changes directory; relative paths require review.")
    return BashAnalysis(
        readonly,
        tuple(dict.fromkeys(accesses)),
        tuple(dict.fromkeys(command_texts)),
        " ".join(dict.fromkeys(reasons)),
    )


def get_readonly_paths(command: str, cwd: Path | None = None) -> list[str] | None:
    """Compatibility API backed by the same analysis used by BashTool."""
    analysis = analyze_bash_command(command, cwd if cwd is not None else Path.cwd())
    if not analysis.readonly:
        return None
    return list(dict.fromkeys(path for operation, path in analysis.paths if operation == "Read")) or ["."]
