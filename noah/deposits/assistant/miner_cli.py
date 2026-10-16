"""Installed participant commands for the Noah miner's contribution workflow."""

from dataclasses import asdict
import json
import configparser
from pathlib import Path
import re
import os
import sys

from ...chain.sources import QuietArgumentParser, Refusal, endpoint_label
from . import cli
from .config import _SECRETISH, load_config


def display_ini(path):
    """Show actual sections, including incomplete settings, without opening keys."""
    ini = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=('#', ';'))
    try:
        with Path(path).open() as stream:
            ini.read_file(stream)
    except (OSError, configparser.Error):
        raise Refusal('Cannot read the settings INI; check its path and syntax.') from None
    # Do not echo comments: they may contain pasted credentials. Secret references
    # and public addresses remain visible; actual signing material never belongs here.
    for section in [ini.default_section, *ini.sections()]:
        values = ini.defaults() if section == ini.default_section else ini[section]
        for key, value in list(values.items()):
            normalized = re.sub(r'[^a-z0-9]', '', key.lower())
            public_metadata = section == 'signing' and key in ('secret_type',)
            if ('\n' in value or (not public_metadata and
                    any(word in normalized for word in (*_SECRETISH, 'passwd', 'token', 'apikey',
                                                        'credential', 'authorization', 'keymaterial', 'signingkey')))):
                values[key] = '[REDACTED]'
            elif 'endpoint' in key.lower() or value.lower().startswith(('ws://', 'wss://', 'http://', 'https://')):
                values[key] = endpoint_label(value)
    print('# Active INI (credentials and endpoint URL details redacted; display only)')
    ini.write(sys.stdout)
    return 0


def _parser(common):
    parser = QuietArgumentParser(
        prog='noah-miner', allow_abbrev=False, parents=[common],
        description='Preview and send Ark contributions using your settings file.',
        epilog='Use --config PATH with any command. Sending asks for confirmation; '
               'previews never unlock a signing wallet or send a transaction.',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init-config', help='write an example INI without overwriting')
    config = commands.add_parser('config', help='show the active INI, with credentials redacted')
    config.add_argument('action', nargs='?', choices=('edit',), help='edit and validate a writable INI')
    config.add_argument('--json', action='store_true', help='validated settings as JSON')
    commands.add_parser('check-config', help='validate settings without contacting the chain')
    commands.add_parser('preview', help='preview a deposit without sending')
    deposit = commands.add_parser('deposit', help='preview, confirm and send one deposit')
    deposit.add_argument('--preview', action='store_true', help='only preview; do not send')
    deposit.add_argument('--label', help='name this intended fixed deposit for safe retries')
    mixed = commands.add_parser('convert-deposit', help='test-only conversion followed by NOAH and foreign deposits')
    mixed_state = mixed.add_mutually_exclusive_group()
    mixed_state.add_argument('--preview', action='store_true', help='read-only mixed plan')
    mixed_state.add_argument('--close-failed', action='store_true', help='explicitly close a verified early conversion failure; never retry')
    mixed_state.add_argument('--status', action='store_true', help='reconcile this mixed intent without sending')
    mixed.add_argument('--label', default='default', help='stable label for this complete workflow')
    mixed.add_argument('--journal', help='shared durable mixed journal for this owner')
    purchase = commands.add_parser('purchase-deposit', help='test-only fixed free-TAO purchase and attributed deposits')
    purchase_state = purchase.add_mutually_exclusive_group()
    purchase_state.add_argument('--preview', action='store_true', help='inspect fixed plan without signing')
    purchase_state.add_argument('--status', action='store_true', help='reconcile without sending')
    purchase_state.add_argument('--cancel-unsubmitted', action='store_true',
                               help='permanently close only a proven pre-PREPARE proxy attempt')
    purchase_state.add_argument('--recover-pre-submit', action='store_true',
                               help='rebind a proven hashless pre-submit reservation to a fresh quote')
    purchase.add_argument('--label', required=True, help='stable label for this fixed workflow')
    purchase.add_argument('--journal', help='shared durable purchase journal for this owner')
    commands.add_parser('status', help='read deposit receipts and reconcile pending results')
    signer = commands.add_parser('signer', help='serve bounded proxy requests in a separate process')
    signer.add_argument('--journal', help='shared durable signer state directory')
    signer.add_argument('--reconcile', action='store_true', help='read uncertain receipts without loading keys')
    resolve = commands.add_parser('resolve', help='record credited total for an existing intent')
    resolve.add_argument('intent_id')
    resolve.add_argument('--credited-before-rao', required=True, type=int)
    return parser


def main(argv=None, *, transaction_runner=None, default_config=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    runner = transaction_runner or cli.main
    common = QuietArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument('--config', default=default_config or
                        os.environ.get('NOAH_DEPOSIT_CONFIG', 'noah-deposit.ini'))
    settings, remaining = common.parse_known_args(argv)
    args = _parser(common).parse_args(remaining)
    path = settings.config
    try:
        if args.command == 'config' and args.action == 'edit':
            if args.json:
                raise Refusal('config edit cannot be combined with --json.')
            from .config_edit import edit_ini
            return edit_ini(path)
        if args.command == 'config' and not args.json:
            return display_ini(path)
        if args.command == 'purchase-deposit':
            if transaction_runner is None:
                from .purchase_runtime import main as purchase_main
                return purchase_main(path,label=args.label,preview=args.preview,
                                     status=args.status,journal_path=args.journal,cancel_unsubmitted=args.cancel_unsubmitted,
                                     recover_pre_submit=args.recover_pre_submit)
            forwarded=['--config',path,'purchase-deposit','--label',args.label]
            if args.preview:forwarded.append('--preview')
            if args.status:forwarded.append('--status')
            if args.cancel_unsubmitted:forwarded.append('--cancel-unsubmitted')
            if args.recover_pre_submit:forwarded.append('--recover-pre-submit')
            if args.journal:forwarded += ['--journal',args.journal]
            return runner(forwarded)
        if args.command == 'convert-deposit':
            if transaction_runner is None:
                from .mixed_runtime import main as mixed_main
                return mixed_main(path,label=args.label,preview=args.preview,
                                  status=args.status,journal_path=args.journal,close_failed=args.close_failed)
            forwarded=['--config',path,'convert-deposit','--label',args.label]
            if args.preview:forwarded.append('--preview')
            if args.status:forwarded.append('--status')
            if args.close_failed:forwarded.append('--close-failed')
            if args.journal:forwarded += ['--journal',args.journal]
            return runner(forwarded)
        if args.command == 'signer':
            from .signer import main as signer_main
            options = ['--config', path]
            if args.journal:
                options += ['--journal', args.journal]
            if args.reconcile:
                options.append('--reconcile')
            return signer_main(options)
        if args.command in ('config', 'check-config'):
            config = load_config(path)
            ini=configparser.ConfigParser(interpolation=None)
            ini.read(path)
            mixed_settings=None
            if ini.has_section('mixed'):
                from .mixed import load_settings,test_gate
                mixed_settings=load_settings(path)
                test_gate(config,mixed_settings)
            purchase_settings=None
            if ini.has_section('purchase'):
                from .purchase import load_settings,test_gate
                purchase_settings=load_settings(path)
                test_gate(config,purchase_settings)
            if args.command == 'check-config':
                result = {'status': 'valid', 'chain_checked': False}
            else:
                result = asdict(config)
                result['endpoint'] = endpoint_label(config.endpoint)
                if mixed_settings:result['mixed']=asdict(mixed_settings)
                if purchase_settings:result['purchase']=asdict(purchase_settings)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        forwarded = ['--config', path]
        if args.command == 'deposit':
            forwarded.append('preview' if args.preview else 'run-once')
            if args.label:
                forwarded += ['--label', args.label]
        else:
            forwarded.append(args.command)
        if args.command == 'resolve':
            forwarded += [args.intent_id, '--credited-before-rao', str(args.credited_before_rao)]
        return runner(forwarded)
    except Refusal as error:
        print(f'Stopped.\n  Why: {error}')
        return 2


def entry():
    cli.finish(main())


if __name__ == '__main__':
    entry()
