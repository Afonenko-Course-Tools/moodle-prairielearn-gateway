#!/usr/bin/env python3
"""Проверка существующего version tag перед ручной публикацией Gateway."""
import os
from pathlib import Path
import re
import subprocess
import tomllib


def project_version(metadata: object) -> str:
    match metadata:
        case {'project': {'version': str(version)}}:
            return version
        case _:
            raise ValueError('В pyproject.toml отсутствует строковая project.version')


def git(*args: str) -> str:
    return subprocess.check_output(['git', *args], text=True).strip()


def main() -> None:
    tag = os.environ['RELEASE_TAG']
    version = project_version(tomllib.loads(Path('pyproject.toml').read_text()))
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag) or tag != 'v' + version:
        raise SystemExit('Tag должен совпадать с версией пакета')
    if git('rev-parse', 'HEAD') != git('rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'):
        raise SystemExit('Checkout не совпадает с запрошенным version tag')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', 'HEAD', 'origin/main']).returncode:
        raise SystemExit('Исходный commit выпуска ещё не включён в main')


if __name__ == '__main__':
    main()
