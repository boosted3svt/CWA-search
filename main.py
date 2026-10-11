#!/usr/bin/env python3
"""Command-line front end. Library classes are reusable by a future GUI."""
import argparse
import json
from pathlib import Path
import sys
from config import Settings
from calibre.client import Client, ClientError, LibraryTimeout
from calibre.downloader import download, SUPPORTED_FORMATS
from calibre.matcher import matches
from calibre.parser import FeedError
from calibre.state import History, prune_library, state_lock


def make_client(settings, library):
    return Client(library, settings.timeout, settings.retries, settings.max_pages, settings.max_results)


def choose_format(book, settings, history, requested=None, include_history=False):
    available = history.available(book, include_history)
    if requested:
        available = [a for a in available if a.format == requested.lower()]
    else:
        available = sorted(available, key=lambda a: settings.preferred_formats.index(a.format)
                           if a.format in settings.preferred_formats else len(settings.preferred_formats))
    supported = [a for a in available if a.format in SUPPORTED_FORMATS]
    if not supported:
        raise ClientError('No untried, supported acquisition for this selection; use --include-history to retry')
    return supported[0]


def acquire(book, library, settings, history, fmt=None, include_history=False):
    acquisition = choose_format(book, settings, history, fmt, include_history)
    client = make_client(settings, library)
    try:
        path = download(client, book, acquisition, settings.download_dir, settings.max_download_bytes)
    except (ClientError, OSError):
        history.record(book, acquisition, 'failed')
        raise
    finally:
        client.close()
    history.record(book, acquisition, 'downloaded', path)
    print(f'Downloaded {acquisition.format.upper()}: {path}')


def search(args, settings, history):
    if not args.author and not args.title:
        raise ValueError('Provide --author and/or --title')
    term = ' '.join(value for value in (args.author, args.title) if value)
    results, failures, successes = [], 0, 0
    libraries = settings.libraries()
    if not libraries:
        raise ValueError('Library list is empty')
    for line, library in libraries:
        client = make_client(settings, library)
        try:
            books = client.search(term)
            successes += 1
            selected = [b for b in books if matches(b, args.title, args.author, args.exact)
                        and any(a.format in SUPPORTED_FORMATS for a in history.available(b, args.include_history))]
            known = {b.key for b in results}
            results.extend(b for b in selected if b.key not in known)
            if client.truncated:
                print(f'Configured page/result limit reached at {library.url}', file=sys.stderr)
            if selected and not args.all_libraries:
                break
        except LibraryTimeout:
            failures += 1
            print(f'Timed out: {library.url}', file=sys.stderr)
            if settings.prune_timeouts and not args.keep_timeouts:
                prune_library(settings.libraries_file, line)
                print('Removed timed-out library; previous list saved to .bak', file=sys.stderr)
        except (ClientError, FeedError) as exc:
            failures += 1
            print(f'Search failed at {library.url}: {exc}', file=sys.stderr)
        finally:
            client.close()
    history.remember(results, {'author': args.author, 'title': args.title})
    if args.json:
        print(json.dumps({'results': [b.to_dict() for b in results], 'failed_libraries': failures,
                          'successful_libraries': successes}, indent=2, ensure_ascii=False))
    else:
        for index, book in enumerate(results, 1):
            formats = ', '.join(a.format.upper() for a in history.available(book, args.include_history))
            print(f'{index}. {book.title}\n   Author: {", ".join(book.authors)}\n'
                  f'   Result ID: {book.key} | Calibre ID: {book.book_id or "unknown"}\n'
                  f'   Source: {book.source} | Library: {book.library_id or "default"}\n'
                  f'   Formats: {formats}')
            if book.cover_url:
                print(f'   Cover: {book.cover_url}')
        if not results:
            print('No eligible matches.' if successes else 'No library could be searched successfully.')
    if args.select and results:
        selection = input('Select result number (Enter to cancel): ').strip()
        if not selection:
            return 0
        try:
            index = int(selection)
        except ValueError as exc:
            raise ValueError('Selection must be a result number') from exc
        if not 1 <= index <= len(results):
            raise ValueError('Selection is outside the result list')
        book = results[index - 1]
        library = next(lib for _, lib in libraries if lib.url == book.source and lib.library_id == book.library_id)
        options = history.available(book, args.include_history)
        print('Formats: ' + ', '.join(a.format for a in options))
        default = choose_format(book, settings, history, include_history=args.include_history).format
        fmt = input(f'Format [{default}]: ').strip() or default
        acquire(book, library, settings, history, fmt, args.include_history)
    return 0 if successes else 1


def cli():
    parser = argparse.ArgumentParser(description='Search configured OPDS libraries and deliberately select downloads.')
    parser.add_argument('--config', default='config.json')
    commands = parser.add_subparsers(dest='command', required=True)
    query = commands.add_parser('search')
    query.add_argument('--author')
    query.add_argument('--title')
    query.add_argument('--exact', action='store_true')
    query.add_argument('--all-libraries', action='store_true')
    query.add_argument('--keep-timeouts', action='store_true')
    query.add_argument('--include-history', action='store_true')
    display = query.add_mutually_exclusive_group()
    display.add_argument('--json', action='store_true')
    display.add_argument('--select', action='store_true')
    fetch = commands.add_parser('download')
    fetch.add_argument('--id', required=True, help='Result ID printed by search; scoped to server and library')
    fetch.add_argument('--format')
    fetch.add_argument('--include-history', action='store_true')
    bad = commands.add_parser('mark-bad')
    bad.add_argument('--id', required=True)
    bad.add_argument('--format', help='Mark only this format; omit to mark all acquisitions')
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
        with state_lock(settings.history_file):
            history = History(settings.history_file)
            if args.command == 'search':
                return search(args, settings, history)
            book = history.book(args.id)
            if args.command == 'mark-bad':
                acquisitions = [a for a in book.acquisitions if not args.format or a.format == args.format.lower()]
                if not acquisitions:
                    raise ValueError('Selected format is not available')
                for acquisition in acquisitions:
                    history.record(book, acquisition, 'bad')
                print('Marked bad; future searches skip these acquisitions.')
                return 0
            library = next((lib for _, lib in settings.libraries()
                            if lib.url == book.source and lib.library_id == book.library_id), None)
            if library is None:
                raise ValueError('Result source is no longer in the library list')
            acquire(book, library, settings, history, args.format, args.include_history)
            return 0
    except (ValueError, KeyError, OSError, ClientError, FeedError) as exc:
        # Never print raw requests exceptions (URLs can contain sensitive parameters).
        message = 'Unknown result ID; run search first' if isinstance(exc, KeyError) else str(exc)
        print(f'Error: {message}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(cli())
