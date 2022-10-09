import json
import os
import time
import sys
import logging
from datetime import datetime
from pathlib import Path

from pyicloud import PyiCloudService as _PyiCloudService
from pyicloud.base import DriveService as _DriveService
import click


logger = logging.getLogger(__name__)


class DriveService(_DriveService):
    # patch: https://github.com/picklepete/pyicloud/issues/337
    def _update_contentws(self, folder_id, sf_info, document_id, file_object):
        data = {
            "data": {
                "signature": sf_info["fileChecksum"],
                "wrapping_key": sf_info["wrappingKey"],
                "reference_signature": sf_info["referenceChecksum"],
                "size": sf_info["size"],
            },
            "command": "add_file",
            "create_short_guid": True,
            "document_id": document_id,
            "path": {
                "starting_document_id": folder_id,
                "path": os.path.basename(file_object.name),
            },
            "allow_conflict": True,
            "file_flags": {
                "is_writable": True,
                "is_executable": False,
                "is_hidden": False,
            },
            "mtime": int(time.time() * 1000),
            "btime": int(time.time() * 1000),
        }

        # Add the receipt if we have one. Will be absent for 0-sized files
        if sf_info.get("receipt"):
            data["data"].update({"receipt": sf_info["receipt"]})

        request = self.session.post(
            self._document_root + "/ws/com.apple.CloudDocs/update/documents",
            params=self.params,
            headers={"Content-Type": "text/plain"},
            data=json.dumps(data),
        )
        self._raise_if_error(request)
        return request.json()

    def send_file(self, folder_id, file_object):
        """Send new file to iCloud Drive."""
        document_id, content_url = self._get_upload_contentws_url(file_object)

        request = self.session.post(content_url, files={os.path.basename(file_object.name): file_object})
        self._raise_if_error(request)
        content_response = request.json()["singleFile"]
        self._update_contentws(folder_id, content_response, document_id, file_object)


class PyiCloudService(_PyiCloudService):
    # patch: https://github.com/picklepete/pyicloud/pull/359/files
    @_PyiCloudService.drive.getter
    def drive(self):
        """Gets the 'Drive' service."""
        if not self._drive:
            if not 'clientId' in self.params:
                self.params['clientId'] = self.client_id
            self._drive = DriveService(
                service_root=self._get_webservice_url("drivews"),
                document_root=self._get_webservice_url("docws"),
                session=self.session,
                params=self.params,
            )
        return self._drive


@click.command()
@click.argument('path_src', type=click.Path(exists=True,
                                            file_okay=False,
                                            resolve_path=True,
                                            path_type=Path))
@click.argument('path_dst', type=click.Path())
# @click.option('--email', prompt=True,
#                          envvar='ISUBLIME_EMAIL')
# @click.option('--password', prompt=True,
#                             hide_input=True)
@click.option('--log-level', type=click.Choice(['debug', 'info', 'warning', 'error'],
                                               case_sensitive=False),
                             default='info',
                             show_default=True,
                             envvar='ISUBLIME_LOG_LEVEL')
def main(path_src, path_dst, log_level,
         email='mail@honzajavorek.cz', password='...'):
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

    # TODO known issue: will be super slow
    # TODO first it should read files from disk, identify folders, create them remotely, then sync files
    # TODO known issue: won't delete any files or dirs on iCloud