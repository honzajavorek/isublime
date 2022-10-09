import sys
import logging
from datetime import datetime
from pathlib import Path

import click

from isublime.icloud import PyiCloudService


logger = logging.getLogger(__name__)


@click.command()
@click.argument('path_src', type=click.Path(exists=True,
                                            file_okay=False,
                                            resolve_path=True,
                                            path_type=Path))
@click.argument('path_dst', type=click.Path())
@click.option('--email', prompt=True,
                         envvar='ISUBLIME_EMAIL')
@click.option('--password', prompt=True,
                            hide_input=True,
                            envvar='ISUBLIME_PASSWORD')
@click.option('--log-level', type=click.Choice(['debug', 'info', 'warning', 'error'],
                                               case_sensitive=False),
                             default='info',
                             show_default=True,
                             envvar='ISUBLIME_LOG_LEVEL')
def main(path_src, path_dst, log_level, email, password):
    logging.basicConfig(level=log_level.upper(),
                        format='[%(name)s] %(levelname)s: %(message)s')
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

    for path in path_src.glob('**/*'):
        if not path.is_file():
            continue
        if path.name == '.DS_Store':
            continue
        logger.info(path)

        dir_dst = path.relative_to(path_src).parent
        logger.info(f'Ensuring (iCloud)/{path_dst}/{dir_dst}')
        icloud_dir = api.drive
        for part in (Path(path_dst).parts + dir_dst.parts):
            try:
                icloud_dir = icloud_dir[part]
            except KeyError:
                icloud_dir.mkdir(part)
                icloud_dir.data.pop('items', None)  # flushing cache of the node ¯\_(ツ)_/¯
                icloud_dir._children = None  # flushing cache of the node ¯\_(ツ)_/¯
                icloud_dir = icloud_dir[part]

        try:
            icloud_file = icloud_dir[path.name]
        except KeyError:
            logger.info(f'Uploading (iCloud)/{path_dst}/{path.relative_to(path_src)}')
            with path.open(mode='rb') as f:
                icloud_dir.upload(f)
        else:
            path_stat = path.stat()
            should_overwrite = (path_stat.st_size != icloud_file.size or
                                datetime.fromtimestamp(path_stat.st_mtime) > icloud_file.date_modified)
            if should_overwrite:
                logger.info(f'Overwriting (iCloud)/{path_dst}/{path.relative_to(path_src)}')
                icloud_file.delete()
                with path.open(mode='rb') as f:
                    icloud_dir.upload(f)
            else:
                logger.info(f'Keeping (iCloud)/{path_dst}/{path.relative_to(path_src)}')
