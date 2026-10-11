"""
Thin Google Drive v3 client for the recorder (scope drive.file: the app
only sees files and folders it created itself). Every call retries 429/5xx
and network errors with exponential backoff + jitter; a set stop_event ends
the waiting at once (UploadCancelled), so a shutdown is never held up.

The OAuth token is created once with scripts/recorder_drive_login.py and
refreshed automatically here; it must live outside the repo.
"""

import logging
import random
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_ATTEMPTS = 6


class DriveUnavailable(Exception):
    """Drive libraries or credentials are missing - nothing can be uploaded."""


class UploadCancelled(Exception):
    """The process is shutting down; the upload resumes on the next run."""


def http_status(exc: Exception) -> int | None:
    try:
        status = getattr(getattr(exc, "resp", None), "status", None)
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retryable(exc: Exception) -> bool:
    status = http_status(exc)
    if status is not None:
        return status in (408, 429) or status >= 500
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


class DriveClient:

    def __init__(self, token_file: Path, expected_account: str | None = None,
                 stop_event: threading.Event | None = None):
        """expected_account: the Google account the files must go to (e.g. the
        primepiptrade.com account). A token for any other account is refused."""
        self._stop_event = stop_event or threading.Event()
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise DriveUnavailable(f"Google Drive libraries not installed ({exc}) - pip install -r requirements.txt") from exc
        try:
            token_file = Path(token_file)
            if not token_file.is_file():
                raise DriveUnavailable(f"Drive token {token_file} not found - run scripts/recorder_drive_login.py")
            credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if not credentials.valid:
                if credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                    token_file.write_text(credentials.to_json(), encoding="utf-8")
                else:
                    raise DriveUnavailable(f"Drive token {token_file} is invalid - run scripts/recorder_drive_login.py again")
            self._media = MediaFileUpload
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        except DriveUnavailable:
            raise
        except Exception as exc:
            raise DriveUnavailable(f"Drive login failed: {exc!r}") from exc
        self.account = self.account_email()
        if expected_account and (self.account or "").lower() != expected_account.strip().lower():
            raise DriveUnavailable(
                f"Drive token belongs to {self.account}, not {expected_account} - "
                f"run scripts/recorder_drive_login.py and sign in as {expected_account}"
            )

    def _call(self, label: str, request_factory):
        """Executes a Drive request with retries. Raises the last error, or
        UploadCancelled when the stop event is set."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._stop_event.is_set():
                raise UploadCancelled(label)
            try:
                return request_factory().execute()
            except Exception as exc:
                if attempt == MAX_ATTEMPTS or not _retryable(exc):
                    raise
                delay = min(60.0, 2 ** attempt) * (0.5 + random.random())
                logger.warning(f"[Drive] {label} failed ({exc}); retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
                if self._stop_event.wait(delay):
                    raise UploadCancelled(label) from exc

    def _create(self, label: str, request_factory, find_existing):
        """Like _call, for requests that CREATE something (not idempotent).
        A timeout or 5xx can hide a create that did succeed on Google's side,
        so before every retry the item is looked up first (find_existing()
        -> item or None) and returned if it is already there - a retry
        never makes a duplicate."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._stop_event.is_set():
                raise UploadCancelled(label)
            if attempt > 1:
                try:
                    existing = find_existing()
                except UploadCancelled:
                    raise
                except Exception as exc:
                    logger.warning(f"[Drive] {label}: could not check for an earlier attempt ({exc}) - retrying")
                    existing = None
                if existing is not None:
                    logger.info(f"[Drive] {label}: the earlier attempt had succeeded - not creating it again")
                    return existing
            try:
                return request_factory().execute()
            except Exception as exc:
                if attempt == MAX_ATTEMPTS or not _retryable(exc):
                    raise
                delay = min(60.0, 2 ** attempt) * (0.5 + random.random())
                logger.warning(f"[Drive] {label} failed ({exc}); retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
                if self._stop_event.wait(delay):
                    raise UploadCancelled(label) from exc

    def account_email(self) -> str | None:
        try:
            about = self._call("account", lambda: self._service.about().get(fields="user(emailAddress)"))
            return about.get("user", {}).get("emailAddress")
        except UploadCancelled:
            raise
        except Exception as exc:
            raise DriveUnavailable(f"Could not read the Drive account: {exc!r}") from exc

    def find_child(self, parent_id: str | None, name: str, folder: bool = False) -> dict | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        conditions = [f"name = '{escaped}'", "trashed = false"]
        conditions.append(f"'{parent_id}' in parents" if parent_id else "'root' in parents")
        if folder:
            conditions.append(f"mimeType = '{FOLDER_MIME}'")
        response = self._call(f"find {name}", lambda: self._service.files().list(
            q=" and ".join(conditions), fields="files(id, name, md5Checksum, size)", spaces="drive", pageSize=10))
        files = response.get("files", [])
        if len(files) > 1:
            logger.warning(f"[Drive] {len(files)} items named {name} in one folder - using the first")
        return files[0] if files else None

    def list_children(self, parent_id: str, folders_only: bool = False) -> list[dict]:
        query = f"'{parent_id}' in parents and trashed = false"
        if folders_only:
            query += f" and mimeType = '{FOLDER_MIME}'"
        items, page_token = [], None
        while True:
            response = self._call("list", lambda: self._service.files().list(
                q=query, fields="nextPageToken, files(id, name, md5Checksum, size, mimeType)",
                pageSize=1000, pageToken=page_token))
            items += response.get("files", [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return items

    def create_folder(self, parent_id: str | None, name: str) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        created = self._create(f"create folder {name}",
                               lambda: self._service.files().create(body=body, fields="id"),
                               lambda: self.find_child(parent_id, name, folder=True))
        return created["id"]

    def folder_exists(self, folder_id: str) -> bool:
        try:
            item = self._call("folder check", lambda: self._service.files().get(fileId=folder_id, fields="id, trashed"))
            return not item.get("trashed", False)
        except UploadCancelled:
            raise
        except Exception:
            return False

    def upload(self, parent_id: str, path: Path, mime_type: str, expected_md5: str | None = None) -> dict:
        """Creates the file. expected_md5 (the local file's md5) lets a retry
        recognise its own earlier, successful attempt."""
        body = {"name": path.name, "parents": [parent_id]}

        def _already_uploaded():
            item = self.find_child(parent_id, path.name)
            if item is not None and (expected_md5 is None or item.get("md5Checksum") == expected_md5):
                return item
            return None

        return self._create(f"upload {path.name}", lambda: self._service.files().create(
            body=body, media_body=self._media(str(path), mimetype=mime_type, resumable=True),
            fields="id, md5Checksum, size"), _already_uploaded)

    def update_content(self, file_id: str, path: Path, mime_type: str) -> dict:
        return self._call(f"update {path.name}", lambda: self._service.files().update(
            fileId=file_id, media_body=self._media(str(path), mimetype=mime_type, resumable=True),
            fields="id, md5Checksum, size"))

    def download_text(self, file_id: str) -> str:
        """Content of a small text file (e.g. a day's _manifest.json)."""
        content = self._call("download", lambda: self._service.files().get_media(fileId=file_id))
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)

    def delete(self, file_id: str) -> None:
        self._call("delete", lambda: self._service.files().delete(fileId=file_id))

    def storage_usage_bytes(self) -> tuple[int, int | None]:
        """(bytes used by the whole Google account - Drive, Gmail, Photos -,
        account limit or None). Informational only."""
        about = self._call("quota", lambda: self._service.about().get(fields="storageQuota"))
        quota = about.get("storageQuota", {})
        limit = quota.get("limit")
        return int(quota.get("usage", 0)), int(limit) if limit else None
