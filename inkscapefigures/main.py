#!/usr/bin/env python3

import os
import logging
import subprocess
from pathlib import Path
from shutil import copy
from daemonize import Daemonize
import click
import platform
from .picker import pick
import pyperclip
from appdirs import user_config_dir

SYSTEM_NAME = platform.system()

try:
    import inotify.adapters
    from inotify.constants import IN_CLOSE_WRITE
    HAS_INOTIFY = True
except ImportError:
    HAS_INOTIFY = False

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
log = logging.getLogger('inkscape-figures')

def inkscape(path):
    subprocess.Popen(['inkscape', str(path)])

def indent(text, indentation=0):
    lines = text.split('\n');
    return '\n'.join(" " * indentation + line for line in lines)

def beautify(name):
    return name.replace('_', ' ').replace('-', ' ').title()

def latex_template(name, title):
    return '\n'.join((
        r"\begin{figure}[ht]",
        r"    \centering",
        rf"    \incfig{{{name}}}",
        rf"    \caption{{{title}}}",
        rf"    \label{{fig:{name}}}",
        r"\end{figure}"))

# From https://stackoverflow.com/a/67692
def import_file(name, path):
    import importlib.util as util
    spec = util.spec_from_file_location(name, path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load user config

user_dir = Path(user_config_dir("inkscape-figures", "Castel"))

if not user_dir.is_dir():
    user_dir.mkdir()

roots_file =  user_dir / 'roots'
template = user_dir / 'template.svg'
config = user_dir / 'config.py'

if not roots_file.is_file():
    roots_file.touch()

if not template.is_file():
    source = str(Path(__file__).parent / 'template.svg')
    destination = str(template)
    copy(source, destination)

if config.exists():
    config_module = import_file('config', config)
    latex_template = config_module.latex_template


def add_root(path):
    path = str(path)
    roots = get_roots()
    if path in roots:
        return None

    roots.append(path)
    roots_file.write_text('\n'.join(roots))


def get_roots():
    return [root for root in roots_file.read_text().split('\n') if root != '']


@click.group()
def cli():
    pass


@cli.command()
@click.option('--daemon/--no-daemon', default=True)
def watch(daemon):
    """
    Watches for figures.
    """
    if HAS_INOTIFY:
        watcher_cmd = watch_daemon_inotify
    else:
        watcher_cmd = watch_daemon_fswatch

    if daemon:
        daemon = Daemonize(app='inkscape-figures',
                           pid='/tmp/inkscape-figures.pid',
                           action=watcher_cmd)
        daemon.start()
        log.info("Watching figures.")
    else:
        log.info("Watching figures.")
        watcher_cmd()


def maybe_recompile_figure(filepath):
    filepath = Path(filepath)
    # A file has changed
    if filepath.suffix != '.svg':
        log.debug('File has changed, but is nog an svg {}'.format(
            filepath.suffix))
        return

    log.info('Recompiling %s', filepath)

    pdf_path = filepath.parent / (filepath.stem + '.pdf')
    name = filepath.stem

    command = [
        'inkscape',
        '--export-area-page',
        '--export-dpi', '300',
        '--export-pdf', pdf_path,
        '--export-latex', filepath
    ]

    log.debug('Running command:')
    log.debug(' '.join(str(e) for e in command))

    # Recompile the svg file
    completed_process = subprocess.run(command)

    if completed_process.returncode != 0:
        log.error('Return code %s', completed_process.returncode)
    else:
        log.debug('Command succeeded')

    # Copy the LaTeX code to include the file to the clipboard
    pyperclip.copy(latex_template(name, beautify(name)))


def watch_daemon_inotify():
    while True:
        roots = get_roots()

        # Watch the file with contains the paths to watch
        # When this file changes, we update the watches.
        i = inotify.adapters.Inotify()
        i.add_watch(str(roots_file), mask=IN_CLOSE_WRITE)

        # Watch the actual figure directories
        log.info('Watching directories: ' + ', '.join(get_roots()))
        for root in roots:
            try:
                i.add_watch(root, mask=IN_CLOSE_WRITE)
            except Exception:
                log.debug('Could not add root %s', root)

        for event in i.event_gen(yield_nones=False):
            (_, type_names, path, filename) = event

            # If the file containing figure roots has changes, update the
            # watches
            if path == str(roots_file):
                log.info('The roots file has been updated. Updating watches.')
                for root in roots:
                    try:
                        i.remove_watch(root)
                        log.debug('Removed root %s', root)
                    except Exception:
                        log.debug('Could not remove root %s', root)
                # Break out of the loop, setting up new watches.
                break

            # A file has changed
            path = Path(path) / filename
            maybe_recompile_figure(path)


def watch_daemon_fswatch():
    while True:
        roots = get_roots()
        log.info('Watching directories: ' + ', '.join(roots))
        # Watch the figures directories, as weel as the config directory
        # containing the roots file (file containing the figures to the figure
        # directories to watch). If the latter changes, restart the watches.
        p = subprocess.Popen(
                ['fswatch', *roots, str(user_dir)], stdout=subprocess.PIPE,
                universal_newlines=True)

        while True:
            filepath = p.stdout.readline().strip()

            # If the file containing figure roots has changes, update the
            # watches
            if filepath == str(roots_file):
                log.info('The roots file has been updated. Updating watches.')
                p.terminate()
                log.debug('Removed main watch %s')
            maybe_recompile_figure(filepath)



@cli.command()
@click.argument('title')
@click.argument(
    'root',
    default=os.getcwd(),
    type=click.Path(exists=False, file_okay=False, dir_okay=True)
)
def create(title, root):
    """
    Creates a figure.

    First argument is the title of the figure
    Second argument is the figure directory.

    """
    title = title.strip()
    file_name = title.replace(' ', '-').lower() + '.svg'
    figures = Path(root).absolute()
    if not figures.exists():
        figures.mkdir()

    figure_path = figures / file_name

    # If a file with this name already exists, append a '2'.
    if figure_path.exists():
        print(title + ' 2')
        return

    copy(str(template), str(figure_path))
    add_root(figures)
    inkscape(figure_path)

    # Print the code for including the figure to stdout.
    # Copy the indentation of the input.
    leading_spaces = len(title) - len(title.lstrip())
    print(indent(latex_template(figure_path.stem, title), indentation=leading_spaces))

@cli.command()
@click.argument(
    'root',
    default=os.getcwd(),
    type=click.Path(exists=True, file_okay=False, dir_okay=True)
)
def edit(root):
    """
    Edits a figure.

    First argument is the figure directory.
    """

    figures = Path(root).absolute()

    # Find svg files and sort them
    files = figures.glob('*.svg')
    files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    # Open a selection dialog using a gui picker like rofi
    names = [beautify(f.stem) for f in files]
    _, index, selected = pick(names)
    if selected:
        path = files[index]
        add_root(figures)
        inkscape(path)

if __name__ == '__main__':
    cli()
