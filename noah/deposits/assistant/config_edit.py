"""Edit a private draft, validate it locally, then replace the settings atomically."""
import configparser
import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import uuid

from ...chain.sources import Refusal
from .config import load_config


def validate_ini(path):
    config = load_config(str(path))
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(path)
    if ini.has_section('mixed'):
        from .mixed import load_settings, test_gate
        test_gate(config, load_settings(path))
    if ini.has_section('purchase'):
        from .purchase import load_settings, test_gate
        test_gate(config, load_settings(path))
    if ini.has_section('signer'):
        from .signer import Signer, ini_policy
        Signer(config, None, ini_policy(path), None)
    return config


def edit_file(path, validate, *, run=subprocess.run):
    """Never invoke a shell, expose file contents, or replace an invalid draft."""
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise Refusal('Edit requires an existing regular settings file, not a symlink.')
    if (not path.stat().st_mode & 0o222 or not os.access(path, os.W_OK)
            or os.statvfs(path).f_flag & getattr(os, 'ST_RDONLY', 1)):
        raise Refusal('This settings file is read-only. Edit its source through the deployment operator.')
    original = path.read_bytes()
    before = hashlib.sha256(original).digest()
    try:
        editor = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'nano')
        if not editor:
            raise ValueError('empty editor')
    except ValueError:
        raise Refusal('Set EDITOR or VISUAL to an editor executable and its arguments.') from None
    # A private directory lets editors use atomic save without affecting the original.
    with tempfile.TemporaryDirectory(prefix='.noah-edit-', dir=path.parent) as directory:
        draft = Path(directory) / path.name
        draft.write_bytes(original)
        draft.chmod(0o600)
        try:
            result = run([*editor, str(draft)], check=False)
        except OSError:
            raise Refusal('Cannot launch the editor; set EDITOR or VISUAL.') from None
        if result.returncode:
            raise Refusal('Editor exited without success; settings were not changed.')
        if draft.is_symlink() or not draft.is_file():
            raise Refusal('Editor must leave a regular settings file.')
        edited = draft.read_bytes()
        if edited == original:
            print('No changes.')
            return 0
        try:
            validate(draft)
        except (Exception, Refusal):
            raise Refusal('Edited settings failed validation; the original file is unchanged.') from None
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).digest() != before:
            raise Refusal('Settings changed while the editor was open; original edits were not overwritten.')
        backup = path.with_name(path.name + '.before-edit-' + uuid.uuid4().hex)
        with backup.open('xb') as stream:
            os.chmod(backup, 0o600)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        # Preserve only ordinary access permissions; never transfer special bits.
        draft.chmod(stat.S_IMODE(path.stat().st_mode) & 0o777)
        with draft.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(draft, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        print(f'Settings saved. Backup: {backup}\nNo transaction was sent.')
    return 0


def edit_ini(path):
    return edit_file(path, validate_ini)
