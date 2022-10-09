import json
import os
import time

from pyicloud import PyiCloudService as _PyiCloudService
from pyicloud.base import DriveService as _DriveService


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
