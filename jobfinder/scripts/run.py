"""Host-neutral entry point; never writes to the installed skill directory."""
import argparse
import os
from pathlib import Path
import shutil
import sys


def main():
    # Redirected output on English Windows otherwise defaults to a legacy codec.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description='JobFinder portable skill runner')
    parser.add_argument('--workspace', required=True, help='Writable task directory, separate from this skill')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='init, or a jobfinder CLI command')
    args = parser.parse_args()
    skill = Path(__file__).resolve().parents[1]
    runtime = skill / 'scripts/runtime'
    if not runtime.is_dir():
        runtime = skill.parents[2]  # Development checkout only; releases carry runtime/.
    if not (runtime / 'jobfinder/__main__.py').is_file():
        parser.error('Incomplete skill: missing scripts/runtime/jobfinder. Reinstall the complete release.')
    workspace = Path(args.workspace).expanduser().resolve()
    if workspace == skill or workspace.is_relative_to(skill):
        parser.error('Workspace must be outside the installed skill directory.')
    workspace.mkdir(parents=True, exist_ok=True)
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if command == ['init']:
        seed = skill / 'assets/workspace'
        if not seed.is_dir():
            seed = runtime
        copied = []
        for name in ('configs/user.template.json', 'configs/offline.synthetic.json',
                     'tests/fixtures/candidates.synthetic.json', 'tests/fixtures/jobs.synthetic.json'):
            src = seed / name
            if not src.is_file():
                parser.error('Incomplete skill: missing template ' + name)
            dest = workspace / name
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(str(dest.relative_to(workspace)))
        print('Workspace ready: ' + str(workspace))
        print('New files: ' + ', '.join(copied))
        return 0
    if not command:
        parser.error('Provide init or a CLI command; use --help for guidance.')
    os.environ['JOBFINDER_WORKSPACE'] = str(workspace)
    sys.path.insert(0, str(runtime))
    os.chdir(workspace)
    sys.argv = ['jobfinder'] + command
    try:
        from jobfinder.__main__ import main as cli
    except ModuleNotFoundError as exc:
        parser.error(f'Missing dependency {exc.name}; install this skill\'s requirements.txt with the same Python.')
    return cli()


if __name__ == '__main__':
    raise SystemExit(main())
