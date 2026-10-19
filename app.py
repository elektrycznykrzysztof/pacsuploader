import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("fileloader")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path, value):
    path = Path(path)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def normalized_relative_path(raw_path):
    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError("Nieprawidłowa ścieżka pliku.")
    if raw_path.startswith("/") or PureWindowsPath(raw_path).drive:
        raise ValueError("Ścieżka musi być względna.")
    pieces = raw_path.split("/")
    if any(part in ("", ".", "..") for part in pieces):
        raise ValueError("Ścieżka zawiera niedozwolony segment.")
    if len(raw_path.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in pieces):
        raise ValueError("Ścieżka pliku jest zbyt długa.")
    if any(any(ord(char) < 32 for char in part) for part in pieces):
        raise ValueError("Ścieżka zawiera niedozwolone znaki.")
    return "/".join(pieces)


def mount_type_for(path, mountinfo_path="/proc/self/mountinfo"):
    """Return the filesystem type for the deepest mount containing path."""
    path = os.path.realpath(path)
    best_mount = None
    best_type = None
    try:
        with open(mountinfo_path, "r", encoding="utf-8") as mounts:
            for line in mounts:
                left, separator, right = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                post_fields = right.split()
                if len(fields) < 5 or not post_fields:
                    continue
                mount_point = fields[4]
                for escaped, decoded in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                    mount_point = mount_point.replace(escaped, decoded)
                mount_point = os.path.realpath(mount_point)
                try:
                    contained = os.path.commonpath([path, mount_point]) == mount_point
                except ValueError:
                    contained = False
                if contained and (best_mount is None or len(mount_point) > len(best_mount)):
                    best_mount = mount_point
                    best_type = post_fields[0]
    except OSError:
        return None
    return best_type


class NfsMonitor(threading.Thread):
    def __init__(self, nfs_root, interval=10, stale_after=30):
        super().__init__(name="nfs-health-monitor", daemon=True)
        self.nfs_root = Path(nfs_root)
        self.interval = interval
        self.stale_after = stale_after
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._connected = None
        self._checked_at = None
        self._completed_at = None

    def _check(self):
        if not self.nfs_root.is_dir():
            raise OSError(f"Katalog NFS nie jest dostępny: {self.nfs_root}")
        filesystem = mount_type_for(self.nfs_root)
        if not filesystem or not filesystem.startswith("nfs"):
            raise OSError(f"{self.nfs_root} nie jest zamontowany jako NFS.")

        # statvfs forces an NFS request. A short-lived write/read/remove probe
        # also confirms that this container can use the export for transfers.
        os.statvfs(self.nfs_root)
        year_root = self.nfs_root / str(datetime.now().year)
        try:
            year_root.mkdir(mode=0o750)
        except FileExistsError:
            pass
        mode = year_root.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise OSError(f"Nieprawidłowy katalog roczny na NFS: {year_root}")

        probe = year_root / f".fileloader-health-{uuid.uuid4().hex}"
        descriptor = None
        try:
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.write(descriptor, b"ok")
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.read(descriptor, 2) != b"ok":
                raise OSError("Testowy odczyt z NFS zwrócił nieprawidłowe dane.")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            probe.unlink(missing_ok=True)

    def snapshot(self):
        with self._lock:
            connected = self._connected
            checked_at = self._checked_at
            completed_at = self._completed_at
        if completed_at is None:
            return {"state": "checking", "connected": None, "checked_at": None}
        if time.monotonic() - completed_at > self.stale_after:
            return {"state": "disconnected", "connected": False, "checked_at": checked_at}
        return {
            "state": "connected" if connected else "disconnected",
            "connected": bool(connected),
            "checked_at": checked_at,
        }

    def run(self):
        while not self.stop_event.is_set():
            error = None
            try:
                self._check()
                connected = True
            except Exception as caught_error:
                connected = False
                error = caught_error

            with self._lock:
                previous = self._connected
                self._connected = connected
                self._checked_at = utc_now()
                self._completed_at = time.monotonic()

            if connected and previous is not True:
                logger.info("Połączenie z NFS działa prawidłowo.")
            elif not connected and previous is not False:
                logger.warning("NFS jest niedostępny: %s", error)
            self.stop_event.wait(self.interval)


class TransferWorker(threading.Thread):
    def __init__(self, data_root, nfs_root, require_nfs):
        super().__init__(name="nfs-transfer-worker", daemon=True)
        self.data_root = Path(data_root)
        self.jobs_root = self.data_root / "jobs"
        self.nfs_root = Path(nfs_root)
        self.require_nfs = require_nfs
        self.stop_event = threading.Event()

    def run(self):
        self._recover_interrupted_jobs()
        logger.info("Worker transferu uruchomiony; katalog docelowy: %s", self.nfs_root)
        while not self.stop_event.is_set():
            job_dir, job = self._next_job()
            if not job:
                self.stop_event.wait(2)
                continue
            try:
                job["state"] = "transferring"
                job["attempts"] = int(job.get("attempts", 0)) + 1
                # Pin the year on the first transfer attempt so retries resume in
                # the same annual directory, even if the calendar year changes.
                if not job.get("destination_year"):
                    job["destination_year"] = str(datetime.now().year)
                job["updated_at"] = utc_now()
                self._save_job(job_dir, job)
                destination = self._deliver(job_dir, job)
                job["state"] = "delivered"
                job["destination"] = "magazynie NFS" if destination == self.nfs_root else destination.name
                job["error"] = None
                job["next_attempt_at"] = None
                job["updated_at"] = utc_now()
                payload_dir = job_dir / "payload"
                if payload_dir.exists():
                    shutil.rmtree(payload_dir)
                self._save_job(job_dir, job)
                logger.info("Paczka %s została wysłana do %s", job["id"], destination)
            except Exception as error:  # Keep source files and retry after a delay.
                logger.exception("Wysyłka paczki %s nie powiodła się", job.get("id", "?"))
                job["state"] = "waiting"
                job["error"] = str(error)[:500]
                delay = min(30 * (2 ** max(0, int(job.get("attempts", 1)) - 1)), 900)
                job["next_attempt_at"] = time.time() + delay
                job["updated_at"] = utc_now()
                try:
                    self._save_job(job_dir, job)
                except OSError:
                    logger.exception("Nie udało się zapisać stanu paczki %s", job.get("id", "?"))

    def _recover_interrupted_jobs(self):
        for job_dir in self.jobs_root.iterdir():
            job_path = job_dir / "job.json"
            if not job_path.is_file():
                continue
            try:
                job = read_json(job_path)
                if job.get("state") == "transferring":
                    job["state"] = "queued"
                    job["next_attempt_at"] = None
                    job["updated_at"] = utc_now()
                    self._save_job(job_dir, job)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nie udało się odtworzyć stanu %s", job_dir)

    def _next_job(self):
        now = time.time()
        try:
            job_dirs = sorted(self.jobs_root.iterdir(), key=lambda path: path.name)
        except OSError:
            logger.exception("Nie udało się odczytać kolejki paczek")
            return None, None
        for job_dir in job_dirs:
            job_path = job_dir / "job.json"
            if not job_path.is_file():
                continue
            try:
                job = read_json(job_path)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nieprawidłowy plik stanu %s", job_path)
                continue
            if job.get("state") == "queued":
                return job_dir, job
            if job.get("state") == "waiting" and float(job.get("next_attempt_at") or 0) <= now:
                return job_dir, job
        return None, None

    def _save_job(self, job_dir, job):
        atomic_write_json(Path(job_dir) / "job.json", job)

    def _destination(self, job):
        # Legacy destination format from older releases.
        return self.nfs_root / f"{job['name']}-{job['id']}"

    def _is_completed(self, destination, job):
        marker = destination / ".fileloader-transfer.json"
        if not destination.is_dir() or not marker.is_file():
            return False
        try:
            saved = read_json(marker)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return saved.get("job_id") == job["id"] and saved.get("files") == job.get("files")

    def _target_matches(self, target, item):
        if not os.path.lexists(target):
            return False
        relative = item["path"]
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"Ścieżka docelowa już istnieje i nie jest zwykłym plikiem: {relative}")
        if target.stat().st_size != int(item["size"]):
            raise RuntimeError(f"Plik o tej nazwie już istnieje na NFS: {relative}")
        digest = hashlib.sha256()
        with target.open("rb") as target_file:
            while True:
                chunk = target_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise RuntimeError(f"Plik o tej nazwie już istnieje na NFS: {relative}")
        return True

    def _ensure_directory(self, path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o750)
            except FileExistsError:
                pass
            mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"Nie można utworzyć katalogu docelowego: {path}")

    def _ensure_target_parent(self, root, parts):
        current = root
        for part in parts:
            current = current / part
            self._ensure_directory(current)

    def _deliver(self, job_dir, job):
        if not self.nfs_root.is_dir():
            raise RuntimeError(f"Katalog NFS nie jest dostępny: {self.nfs_root}")
        if self.require_nfs:
            filesystem = mount_type_for(self.nfs_root)
            if not filesystem or not filesystem.startswith("nfs"):
                raise RuntimeError(f"{self.nfs_root} nie jest zamontowany jako NFS (wykryto: {filesystem or 'brak'}).")

        year = str(job.get("destination_year") or datetime.now().year)
        if not re.fullmatch(r"[0-9]{4}", year):
            raise RuntimeError("Nieprawidłowy rok katalogu docelowego.")
        year_root = self.nfs_root / year
        self._ensure_directory(year_root)

        # Finish cleanup for a transfer completed by an older application version.
        legacy_destination = self._destination(job)
        if self._is_completed(legacy_destination, job):
            payload_dir = job_dir / "payload"
            if payload_dir.exists():
                shutil.rmtree(payload_dir)
            return legacy_destination

        staging = year_root / f".fileloader-{job['id']}.partial"
        if staging.exists():
            if staging.is_symlink():
                raise RuntimeError("Katalog tymczasowy transferu jest dowiązaniem symbolicznym.")
            shutil.rmtree(staging)
        staging.mkdir(mode=0o750)
        payload_dir = job_dir / "payload"
        for item in job.get("files", []):
            relative = normalized_relative_path(item["path"])
            parts = PurePosixPath(relative).parts
            final_path = year_root.joinpath(*parts)
            self._ensure_target_parent(year_root, parts[:-1])
            if self._target_matches(final_path, item):
                continue

            source = payload_dir.joinpath(*parts)
            staged_path = staging.joinpath(*parts)
            if not payload_dir.is_dir() or source.is_symlink() or not source.is_file():
                raise RuntimeError(f"Brakuje lokalnego pliku: {relative}")
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            copied = 0
            with source.open("rb") as source_file, staged_path.open("wb") as target_file:
                while True:
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    target_file.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                target_file.flush()
                os.fsync(target_file.fileno())
            if copied != int(item["size"]) or digest.hexdigest() != item["sha256"]:
                raise RuntimeError(f"Weryfikacja pliku nie powiodła się: {relative}")

            if not self._target_matches(final_path, item):
                try:
                    # A hard link publishes the fully verified file atomically and
                    # fails safely if another writer created the name meanwhile.
                    os.link(staged_path, final_path)
                except FileExistsError:
                    self._target_matches(final_path, item)
                staged_path.unlink(missing_ok=True)
            if not self._target_matches(final_path, item):
                raise RuntimeError(f"Nie udało się potwierdzić pliku na NFS: {relative}")

        shutil.rmtree(staging)
        return year_root


def create_app():
    data_root = Path(os.environ.get("DATA_DIR", "/data"))
    upload_temp_root = data_root / ".upload-tmp"
    upload_temp_root.mkdir(parents=True, exist_ok=True)
    # Werkzeug spools multipart file bodies to tempfile once they exceed its
    # in-memory threshold. Keep those files on the persistent data volume rather
    # than the container's size-limited /tmp tmpfs.
    os.environ["TMPDIR"] = str(upload_temp_root)
    tempfile.tempdir = str(upload_temp_root)

    # Remove temporary parser files left behind by an interrupted container.
    for stale_temp in upload_temp_root.iterdir():
        if stale_temp.is_dir() and not stale_temp.is_symlink():
            shutil.rmtree(stale_temp, ignore_errors=True)
        else:
            stale_temp.unlink(missing_ok=True)

    app = Flask(__name__, static_folder="static", template_folder="templates")
    nfs_root = Path(os.environ.get("NFS_DIR", "/mnt/nfs"))
    require_nfs = os.environ.get("REQUIRE_NFS", "true").lower() in ("1", "true", "yes", "on")
    app_token = os.environ.get("APP_TOKEN", "")
    if not app_token:
        raise RuntimeError("Ustaw APP_TOKEN w pliku .env przed uruchomieniem aplikacji.")

    jobs_root = data_root / "jobs"
    incoming_root = data_root / ".incoming"
    jobs_root.mkdir(parents=True, exist_ok=True)
    incoming_root.mkdir(parents=True, exist_ok=True)

    # A request interrupted before it becomes a queued job is not accepted data.
    # Clear such partial uploads on process start; committed jobs live in jobs/.
    for abandoned_upload in incoming_root.iterdir():
        if abandoned_upload.is_dir():
            shutil.rmtree(abandoned_upload, ignore_errors=True)
        else:
            abandoned_upload.unlink(missing_ok=True)

    max_upload_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024 * 1024)))
    app.config["MAX_CONTENT_LENGTH"] = max_upload_bytes
    app.config["MAX_FORM_MEMORY_SIZE"] = 64 * 1024 * 1024
    app.config["MAX_FORM_PARTS"] = 20001

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.before_request
    def require_api_token():
        if request.path.startswith("/api/") and request.path != "/api/health":
            provided = request.headers.get("X-App-Token", "")
            if not hmac.compare_digest(provided, app_token):
                return jsonify({"error": "Nieprawidłowy token dostępu."}), 401

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "nfs": nfs_monitor.snapshot()})

    @app.get("/api/jobs")
    def list_jobs():
        jobs = []
        for job_dir in jobs_root.iterdir():
            path = job_dir / "job.json"
            if not path.is_file():
                continue
            try:
                job = read_json(path)
                jobs.append(job)
            except (OSError, ValueError, json.JSONDecodeError):
                logger.exception("Nie udało się odczytać stanu %s", path)
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        active_count = sum(job.get("state") == "transferring" for job in jobs)
        return jsonify({"jobs": jobs[:100], "active_count": active_count})

    @app.post("/api/upload")
    def upload():
        uploaded_files = request.files.getlist("files")
        raw_paths = request.form.getlist("paths")
        if not uploaded_files:
            return jsonify({"error": "Wybierz co najmniej jeden plik."}), 400
        if raw_paths and len(raw_paths) != len(uploaded_files):
            return jsonify({"error": "Nieprawidłowa lista ścieżek plików."}), 400
        if not raw_paths:
            raw_paths = [file.filename or "plik" for file in uploaded_files]
        if len(uploaded_files) > 10000:
            return jsonify({"error": "Paczka zawiera zbyt wiele plików (limit: 10 000)."}), 400

        try:
            relative_paths = [normalized_relative_path(value) for value in raw_paths]
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        path_set = {tuple(PurePosixPath(value).parts) for value in relative_paths}
        if len(path_set) != len(relative_paths):
            return jsonify({"error": "Paczka zawiera powtórzone ścieżki plików."}), 400
        for parts in path_set:
            if any(parts[:index] in path_set for index in range(1, len(parts))):
                return jsonify({"error": "Ścieżka pliku koliduje z katalogiem w paczce."}), 400

        job_id = uuid.uuid4().hex
        job_dir = incoming_root / job_id
        payload_dir = job_dir / "payload"
        payload_dir.mkdir(parents=True, exist_ok=False)
        stored_files = []
        total_bytes = 0
        try:
            for uploaded, relative in zip(uploaded_files, relative_paths):
                target = payload_dir.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with target.open("wb") as output:
                    while True:
                        chunk = uploaded.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                total_bytes += size
                stored_files.append({"path": relative, "size": size, "sha256": digest.hexdigest()})

            job = {
                "id": job_id,
                "name": PurePosixPath(relative_paths[0]).parts[0],
                "state": "queued",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "attempts": 0,
                "error": None,
                "next_attempt_at": None,
                "destination": None,
                "total_bytes": total_bytes,
                "files": stored_files,
            }
            atomic_write_json(job_dir / "job.json", job)
            final_job_dir = jobs_root / job_id
            os.replace(job_dir, final_job_dir)
            return jsonify({"job": job}), 201
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return jsonify({"error": "Nie znaleziono paczki."}), 404
        job_dir = jobs_root / job_id
        job_path = job_dir / "job.json"
        if not job_path.is_file():
            return jsonify({"error": "Nie znaleziono paczki."}), 404
        try:
            job = read_json(job_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"error": "Nie udało się odczytać paczki."}), 500
        if job.get("state") == "delivered":
            return jsonify({"error": "Ta paczka została już wysłana."}), 409
        job["state"] = "queued"
        job["next_attempt_at"] = None
        job["error"] = None
        job["updated_at"] = utc_now()
        atomic_write_json(job_path, job)
        return jsonify({"job": job})

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        limit_gb = max_upload_bytes / (1024 ** 3)
        return jsonify({"error": f"Przekroczono limit rozmiaru uploadu ({limit_gb:g} GB)."}), 413

    nfs_monitor = NfsMonitor(nfs_root)
    nfs_monitor.start()
    app.extensions["nfs_monitor"] = nfs_monitor

    worker = TransferWorker(data_root, nfs_root, require_nfs)
    worker.start()
    app.extensions["transfer_worker"] = worker
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
