import sys
import logging
from datetime import datetime
from pathlib import Path
import subprocess
from functools import lru_cache

from pyicloud.exceptions import PyiCloudAPIResponseException
import gevent
from gevent import monkey
import click

from .icloud import PyiCloudService


IGNORED = ('.DS_Store', )

JOBS_BATCH = 10

NODE_CREATION_POLLING_WAIT_SEC = 1


monkey.patch_socket()

logger = logging.getLogger('isublime')


@lru_cache
def op(item_name):
    try:
        proc = subprocess.run(['op', 'item', 'get', item_name,
                               '--fields', 'label=username,label=password'],
                              stdout=subprocess.PIPE,
                              check=True)
    except FileNotFoundError:
        return dict(email=None, password=None)
    else:
        stdout = proc.stdout.decode('utf-8').strip()
        username, password = stdout.split(',')
        return dict(email=username, password=password)


@click.command()
@click.argument('path_src', type=click.Path(exists=True,
                                            file_okay=False,
                                            resolve_path=True,
                                            path_type=Path))
@click.argument('path_dst', type=click.Path())
@click.option('--email', envvar='ISUBLIME_EMAIL')
@click.option('--password', hide_input=True,
                            envvar='ISUBLIME_PASSWORD')
@click.option('--op-item-name', default='Apple ID')
@click.option('--log-level', type=click.Choice(['debug', 'info', 'warning', 'error'],
                                               case_sensitive=False),
                             default='info',
                             show_default=True,
                             envvar='ISUBLIME_LOG_LEVEL')
def main(path_src, path_dst, log_level, email, password, op_item_name):
    logging.basicConfig(level=log_level.upper(),
                        format='[%(name)s] %(levelname)s: %(message)s')
    email = email or op(op_item_name)['email'] or click.prompt('Apple ID e-mail')
    password = password or op(op_item_name)['password'] or click.prompt('Apple ID password', hide_input=True)

    logger.info(f"Syncing files from {path_src} to (iCloud)/{path_dst.lstrip('/')}")
    api = PyiCloudService(email, password)
    if api.requires_2fa:
        logger.info('2FA required')
        code = click.prompt('Enter the 2FA code you received to one of your approved devices', type=int)
        result = api.validate_2fa_code(code)

        logger.info(f'2FA validation result: {result!r}')
        if not result:
            logger.error('Failed to verify 2FA code')
            sys.exit(1)

        if not api.is_trusted_session:
            logger.error('Session is not trusted, requesting trust')
            result = api.trust_session()
            logger.info(f'Session trust result: {result!r}')

            if not result:
                logger.error('Failed to request trust! You will likely be prompted for the code again in the coming weeks')
    logger.info('Logged in')

    logger.debug('Reading source files')
    paths = [path for path in path_src.glob('**/*')
            if path.name not in IGNORED]
    logger.info(f'Found {len(paths)} source files')

    logger.debug('Spawning jobs')
    jobs = []
    for path in paths:
        logger.debug(f'Spawning a job for {path}')
        job = gevent.spawn(sync, api.drive, path_src, path_dst, path)
        jobs.append(job)

        if len(jobs) == len(paths) or len(jobs) >= JOBS_BATCH:
            logger.debug(f'Waiting for a batch of {len(jobs)} jobs')
            gevent.joinall(jobs)
            jobs = []


def sync(icloud, path_src, path_dst, path):
    try:
        logger.info(path)
        path_relative = path.relative_to(path_src)
        dir_dst = path_relative if path.is_dir() else path_relative.parent

        logger.info(f'Ensuring (iCloud)/{path_dst}/{dir_dst}')
        icloud_node = icloud
        for part in (Path(path_dst).parts + dir_dst.parts):
            try:
                icloud_node = icloud_node[part]
            except KeyError:
                logger.debug(f'Node {part!r} does not exist, creating')
                icloud_node.mkdir(part)
                while True:
                    if hasattr(icloud_node, 'root'):
                        logger.debug(f'Flushing cache of the root node {icloud_node.root!r}')
                        icloud_node._root = None
                    else:
                        logger.debug(f'Flushing cache of the parent node {icloud_node!r}')
                        icloud_node.data.pop('items', None)
                        icloud_node._children = None
                    try:
                        logger.debug(f'Getting node {part!r}')
                        icloud_node = icloud_node[part]
                        break
                    except KeyError:
                        logger.debug(f'Node {part!r} not yet created, waiting')
                        gevent.sleep(NODE_CREATION_POLLING_WAIT_SEC)

        if path.is_file():
            try:
                icloud_file = icloud_node[path.name]
            except KeyError:
                logger.info(f'Uploading (iCloud)/{path_dst}/{path_relative}')
                with path.open(mode='rb') as f:
                    icloud_node.upload(f)
            else:
                path_stat = path.stat()
                should_overwrite = (path_stat.st_size != icloud_file.size or
                                    datetime.fromtimestamp(path_stat.st_mtime) > icloud_file.date_modified)
                if should_overwrite:
                    logger.info(f'Overwriting (iCloud)/{path_dst}/{path_relative}')
                    icloud_file.delete()
                    with path.open(mode='rb') as f:
                        icloud_node.upload(f)
                else:
                    logger.info(f'Keeping (iCloud)/{path_dst}/{path_relative}')
    except PyiCloudAPIResponseException as e:
        logger.error(str(e))
        logger.warning('Retrying')
        return sync(icloud, path_src, path_dst, path)
